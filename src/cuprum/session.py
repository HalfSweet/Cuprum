"""Connecting to the KiCad IPC API.

A document that is open in a running KiCad GUI is edited there, live. Otherwise a headless KiCad
server (``kicad-cli api-server``) is used. It stays in the background, one per project: the first
``connect`` starts it and later scripts attach to the same server, which keeps the open documents,
the loaded libraries and any unsaved changes.
"""

from __future__ import annotations

import hashlib
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
from kipy.proto.common.types import DocumentType
from kipy.server import find_kicad_cli

# At the pinned kicad-python commit, create_items / update_items unpack each returned item twice
# (editor.py calls unpack_any, then unwrap() inside _item_from_message unpacks again) and raise
# AttributeError even though KiCad has already applied the change. Passing the Any through
# untouched fixes it, and stays harmless once upstream fixes the bug.
kipy.editor.unpack_any = lambda message: message

_DOCUMENT_TYPES = {
    ".kicad_sch": DocumentType.DOCTYPE_SCHEMATIC,
    ".kicad_pcb": DocumentType.DOCTYPE_PCB,
}

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

    The background server is started when needed and keeps running after the script exits; shut
    it down with :func:`stop`. It opens the ``.kicad_sch`` / ``.kicad_pcb`` at ``path``, or creates
    it when the file does not exist (a new document lives in memory until it is saved). The
    schematic and the board of one project share a server.

    KiCad does not allow opening documents through the API in a GUI, so there the document has
    to be open already.
    """
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
    no path is given, any. ``None`` when there is no such GUI."""
    primary = Path(_default_socket_path().removeprefix("ipc://"))
    # The first KiCad instance listens on api.sock, further ones on api-<pid>.sock.
    for socket in sorted(primary.parent.glob("api*.sock")):
        probe = KiCad(socket_path=f"ipc://{socket}", timeout_ms=1000)
        try:
            if path is None:
                probe.ping()
                found = True
            else:
                found = any(
                    Path(document.project.path) == path.parent
                    and document.project.name == path.stem
                    for document in probe.get_open_documents(doc_type)
                )
        except (ApiError, KiCadConnectionError):
            found = False
        finally:
            probe.close()
        if found:
            return KiCad(socket_path=f"ipc://{socket}", timeout_ms=timeout_ms)
    return None


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
