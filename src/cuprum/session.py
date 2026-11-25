"""Connecting to the KiCad IPC API.

A document that is open in a running KiCad GUI is edited there, live. Otherwise a headless KiCad
server (``kicad-cli api-server``) is used. It stays in the background, one per project: the first
``connect`` starts it and later scripts attach to the same server, which keeps the open documents,
the loaded libraries and any unsaved changes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import kipy.editor
from kipy import KiCad
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.kicad import _default_socket_path
from kipy.proto.common.types import DocumentSpecifier, DocumentType
from kipy.server import find_kicad_cli

_log = logging.getLogger(__name__)

# At the pinned kicad-python commit, create_items / update_items unpack each returned item twice
# (editor.py calls unpack_any, then unwrap() inside _item_from_message unpacks again) and raise
# AttributeError even though KiCad has already applied the change. Passing the Any through
# untouched fixes it, and stays harmless once upstream fixes the bug.
kipy.editor.unpack_any = lambda message: message

_DOCUMENT_TYPES = {
    ".kicad_sch": DocumentType.DOCTYPE_SCHEMATIC,
    ".kicad_pcb": DocumentType.DOCTYPE_PCB,
}
_EDITORS = {
    DocumentType.DOCTYPE_SCHEMATIC: "schematic editor",
    DocumentType.DOCTYPE_PCB: "PCB editor",
}

# What connect() does when its caller does not say: the command line sets this from --gui and
# --headless.
default_headless: bool | None = None


class EditorNotOpen(RuntimeError):
    """A running KiCad has the project open, but not the editor of the document asked for."""


# Under the home directory rather than the temp directory: the location must not depend on
# environment variables, and the path stays short (Unix socket paths have a length limit).
_STATE_DIR = Path.home() / ".cache" / "cuprum"


def connect(
    path: str | Path | None = None,
    *,
    headless: bool | None = None,
    kicad_cli: str | None = None,
    timeout_ms: int = 60_000,
) -> KiCad:
    """Connect to KiCad and return kicad-python's ``KiCad`` object (usable as a context manager).

    By default the document at ``path`` is edited in a running KiCad GUI when one has it open
    (the GUI's API server must be enabled in its preferences), and in the project's background
    server otherwise. ``headless=True`` insists on the background server and ``headless=False``
    on the GUI.

    A GUI that has the project open but not the editor of this document gets no background
    server beside it, unless ``headless=True`` asks for one: two KiCads on one project overwrite
    each other's project settings, and the user who opened it expects to see the work. That
    case raises :class:`EditorNotOpen`.

    The background server is started when needed and keeps running after the script exits; shut
    it down with :func:`stop`. It opens the ``.kicad_sch`` / ``.kicad_pcb`` at ``path``, or creates
    it when the file does not exist (a new document lives in memory until it is saved). The
    schematic and the board of one project share a server.

    KiCad does not allow opening documents through the API in a GUI, so there the document has
    to be open already.
    """
    if headless is None:
        headless = default_headless
    doc_type = None
    if path is not None:
        path = Path(path).expanduser().resolve()
        doc_type = _DOCUMENT_TYPES.get(path.suffix)
        if doc_type is None:
            raise ValueError(f"unsupported document type: {path.name}")

    if headless is not True:
        gui = _gui(path, doc_type, timeout_ms)
        if gui is not None:
            _close_in_server(path, doc_type)
            return gui
        if headless is False:
            what = "no running KiCad GUI" if path is None else f"no KiCad GUI has {path.name} open"
            raise RuntimeError(f"{what} (is the API server enabled in its preferences?)")

    socket, pid_file, log_file = _server_files(path)
    if not _alive(socket):
        _start_server(socket, pid_file, log_file, kicad_cli, path.parent if path else None)
        started = "started a background KiCad server"
        if path is not None:
            started += f" for {path.stem}"
            if headless is None:
                started += f": no running KiCad has {path.name} open"
        _log.info(started)

    kicad = KiCad(socket_path=f"ipc://{socket}", timeout_ms=timeout_ms)
    try:
        if path is not None and not _is_open(kicad, doc_type):
            if path.exists():
                kicad.open_document(str(path), doc_type)
            else:
                kicad.create_document(str(path), doc_type)
        # Placing symbols needs the libraries; both calls are nearly free once they are loaded.
        kicad.load_all_libraries()
        kicad.wait_for_libraries()
    except BaseException:
        kicad.close()
        raise
    return kicad


def _close_in_server(path: Path | None, doc_type) -> None:
    """Make the background server let go of a document that is now edited in the GUI, so that
    there is one copy of it in memory. The server stays up for the project's other document."""
    socket = _server_files(path)[0]
    if path is None or not _alive(socket):
        return
    kicad = KiCad(socket_path=f"ipc://{socket}", timeout_ms=5000)
    try:
        for document in kicad.get_open_documents(doc_type):
            kicad.close_document(document)
    except ApiError:
        pass  # no document of this type is open there
    finally:
        kicad.close()


def _gui(path: Path | None, doc_type, timeout_ms: int) -> KiCad | None:
    """A connection to a running KiCad GUI: one that has the document at ``path`` open or, when
    no path is given, any. ``None`` when there is no such GUI. Raises :class:`EditorNotOpen`
    when one has the project of ``path`` open without the document."""
    primary = Path(_default_socket_path().removeprefix("ipc://"))
    without_editor = False
    # The first KiCad instance listens on api.sock, further ones on api-<pid>.sock.
    for socket in sorted(primary.parent.glob("api*.sock")):
        probe = KiCad(socket_path=f"ipc://{socket}", timeout_ms=1000)
        try:
            probe.ping()
            found = path is None or _has_document(probe, path, doc_type)
            if not found and _has_project(probe, path):
                without_editor = True
        except (ApiError, KiCadConnectionError):
            found = False  # a socket that a KiCad left behind
        finally:
            probe.close()
        if found:
            return KiCad(socket_path=f"ipc://{socket}", timeout_ms=timeout_ms)
    if without_editor:
        editor = _EDITORS[doc_type]
        raise EditorNotOpen(
            f"KiCad has the project {path.stem} open but not its {editor}: open the {editor} "
            "there (--headless uses a background server beside it instead)"
        )
    return None


def _has_document(kicad: KiCad, path: Path, doc_type) -> bool:
    try:
        documents = kicad.get_open_documents(doc_type)
    except ApiError:
        return False  # KiCad has no handler for the request while no editor of the kind is open
    return any(
        Path(document.project.path).resolve() == path.parent and document.project.name == path.stem
        for document in documents
    )


def _has_project(kicad: KiCad, path: Path) -> bool:
    """Whether this KiCad has the project of the document at ``path`` open. The project manager
    answers no request that names its project, but it expands the variables that hold the
    project's directory and name."""
    project = kicad.get_project(DocumentSpecifier(type=DocumentType.DOCTYPE_PROJECT))
    try:
        loaded = project.expand_text_variables("${KIPRJMOD}/${PROJECTNAME}", expand_env_vars=True)
    except ApiError:
        return False
    return Path(loaded).resolve() == path.with_suffix("")


def stop(path: str | Path | None = None) -> bool:
    """Shut down the background KiCad server that :func:`connect` started for this project.

    Returns whether a server was actually running. Unsaved changes held by the server are lost.
    """
    if path is not None:
        path = Path(path).expanduser().resolve()
    socket, pid_file, _log_file = _server_files(path)

    running = _alive(socket)
    if running:
        pid = int(pid_file.read_text())
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while _alive(socket):
            if time.monotonic() >= deadline:
                os.kill(pid, signal.SIGKILL)
                break
            time.sleep(0.05)
    shutil.rmtree(socket.parent, ignore_errors=True)
    return running


def _server_files(path: Path | None) -> tuple[Path, Path, Path]:
    """The socket, pid file and log file of a project's background server.

    Each server gets its own directory because KiCad drops an ``api.lock`` next to the socket.
    """
    project = "" if path is None else str(path.with_suffix(""))
    directory = _STATE_DIR / hashlib.sha1(project.encode()).hexdigest()[:16]
    return directory / "api.sock", directory / "pid", directory / "log"


def _alive(socket: Path) -> bool:
    if not socket.exists():
        return False
    probe = KiCad(socket_path=f"ipc://{socket}", timeout_ms=1000)
    try:
        probe.ping()
        return True
    except (ApiError, KiCadConnectionError):
        return False
    finally:
        probe.close()


def _start_server(
    socket: Path, pid_file: Path, log_file: Path, kicad_cli: str | None, cwd: Path | None
) -> None:
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    if cwd is not None:
        cwd.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as log:
        process = subprocess.Popen(
            [find_kicad_cli(kicad_cli), "api-server", "--socket", str(socket)],
            cwd=cwd,  # relative export paths then resolve against the project directory
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # detach, so the server outlives this script
        )
    pid_file.write_text(str(process.pid))

    deadline = time.monotonic() + 30.0
    while not _alive(socket):
        if process.poll() is not None or time.monotonic() >= deadline:
            process.kill()
            raise RuntimeError(f"kicad-cli api-server failed to start; see {log_file}")
        time.sleep(0.05)


def _is_open(kicad: KiCad, doc_type) -> bool:
    try:
        return len(kicad.get_open_documents(doc_type)) > 0
    except ApiError:
        # Until a document of this type has been opened, KiCad has no handler for the request.
        return False
