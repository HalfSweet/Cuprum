"""Board-side pieces that kicad-python does not provide yet: syncing the board from the
schematic, DRC, text angles and the stackup.

Coordinates in text output are always in millimetres.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kipy.board import Board
from kipy.board_types import BoardText, Field
from kipy.geometry import Vector2
from kipy.proto.board import board_tasks_pb2
from kipy.proto.board.board_commands_pb2 import NetlistMatchMode
from kipy.proto.board.board_pb2 import BoardStackupDielectricType, BoardStackupLayerType
from kipy.proto.board.board_rules_pb2 import DrcErrorType
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types import RuleSeverity
from kipy.util.board_layer import is_copper_layer

from cuprum.export import export_netlist
from cuprum.session import connect
from cuprum.tasks import run_task

_AT = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")
_TEXT_OPENERS = ("(gr_text ", "(fp_text ", "(property ")
_SETUP = re.compile(r"^\t\(setup\b", re.MULTILINE)
_BOARD_THICKNESS = re.compile(r"(^\t\(general\s+\(thickness\s+)[\d.]+", re.MULTILINE)
_STACKUP_KINDS = {
    BoardStackupLayerType.BSLT_SILKSCREEN: "Silk Screen",
    BoardStackupLayerType.BSLT_SOLDERPASTE: "Solder Paste",
    BoardStackupLayerType.BSLT_SOLDERMASK: "Solder Mask",
}


def _natural(text: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def sync_board(
    schematic_path: str | Path, *, delete_extra: bool = False, by_reference: bool = False
):
    """Update the project's board from its schematic, creating the board if it does not exist.

    This is KiCad's "update PCB from schematic": the netlist is exported from the schematic and
    imported into the board. New footprints are added, changed ones are updated and pad nets are
    reassigned. Footprints that have no symbol are only removed when ``delete_extra`` is set.

    Footprints are matched to symbols by the symbol's identity, so a symbol that was deleted and
    placed again counts as a new one. ``by_reference`` matches by reference instead, which lets
    such a symbol keep its footprint and the footprint's place on the board.

    Returns kicad-python's ``ImportNetlistResult`` and what happened to the footprints:
    ``{"added": [reference, ...], "removed": [...], "changed": [{"reference": ..., "changes":
    [{"property": "value", "pad": None, "before": ..., "after": ...}, ...]}, ...]}``. The net of
    a pad is the property ``"net"`` with the pad's number.
    """
    schematic_path = Path(schematic_path).expanduser().resolve()
    board_path = schematic_path.with_suffix(".kicad_pcb")
    with tempfile.TemporaryDirectory() as directory:
        netlist_path = Path(directory) / "netlist.net"
        with connect(schematic_path) as kicad:
            # The netlist is exported from the file on disk; see cuprum.export.
            kicad.get_schematic().save()
        export_netlist(schematic_path, netlist_path)
        with connect(board_path) as kicad:
            board = kicad.get_board()
            before = _footprint_states(board)
            result = board.import_netlist(
                str(netlist_path),
                match_mode=NetlistMatchMode.NMM_REFERENCE
                if by_reference
                else NetlistMatchMode.NMM_UUID,
                delete_extra_footprints=delete_extra,
            )
            board.save()
            changes = _footprint_changes(before, _footprint_states(board))
    return result, changes


def _footprint_states(board: Board) -> dict[str, dict]:
    return {
        footprint.id.value: {
            "reference": footprint.reference_field.text.value,
            "value": footprint.value_field.text.value,
            "footprint": str(footprint.definition.id),
            "nets": {pad.number: pad.net.name for pad in footprint.definition.pads},
        }
        for footprint in board.get_footprints()
    }


def _footprint_changes(before: dict[str, dict], after: dict[str, dict]) -> dict:
    added = {after[i]["reference"]: after[i] for i in after.keys() - before.keys()}
    removed = {before[i]["reference"]: before[i] for i in before.keys() - after.keys()}
    pairs = [(before[i], after[i]) for i in before.keys() & after.keys()]
    # A footprint that KiCad exchanged for another one comes back under a new identity.
    for reference in added.keys() & removed.keys():
        pairs.append((removed.pop(reference), added.pop(reference)))

    changed = {}
    for old, new in pairs:
        changes = [
            {"property": name, "pad": None, "before": old[name], "after": new[name]}
            for name in ("reference", "value", "footprint")
            if old[name] != new[name]
        ]
        changes += [
            {
                "property": "net",
                "pad": number,
                "before": old["nets"].get(number) or None,
                "after": net or None,
            }
            for number, net in new["nets"].items()
            if old["nets"].get(number) != net
        ]
        if changes:
            changed[new["reference"]] = changes

    return {
        "added": sorted(added, key=_natural),
        "removed": sorted(removed, key=_natural),
        "changed": [
            {"reference": reference, "changes": changed[reference]}
            for reference in sorted(changed, key=_natural)
        ],
    }


@dataclass(frozen=True)
class DrcViolation:
    type: str
    """For example ``"UNCONNECTED_ITEMS"`` or ``"CLEARANCE"``."""
    severity: str
    """For example ``"ERROR"`` or ``"WARNING"``."""
    description: str
    position: Vector2
    layer: str
    """For example ``"F.Cu"``; empty when the violation is not tied to a layer."""
    items: tuple[str, ...]
    """The objects involved: pads as ``"R1.2"``, footprints by reference, anything else by ID."""
    excluded: bool

    def __str__(self) -> str:
        layer = f" {self.layer}" if self.layer else ""
        items = f" [{', '.join(self.items)}]" if self.items else ""
        excluded = " (excluded)" if self.excluded else ""
        return (
            f"{self.severity} {self.type} @{format_mm(self.position)}{layer}: "
            f"{self.description}{items}{excluded}"
        )


def run_drc(
    board: Board, *, schematic_parity: bool = True, timeout_s: float = 300.0
) -> list[DrcViolation]:
    """Run DRC on the board and return the violations.

    Unrouted connections are reported as ``UNCONNECTED_ITEMS``. With ``schematic_parity`` the
    board is also compared against the schematic.
    """
    token = _start_drc(board, schematic_parity, timeout_s)

    request = board_tasks_pb2.GetDrcTaskResult()
    request.token.CopyFrom(token)
    result = board.client.send(request, board_tasks_pb2.DrcTaskResult)

    names: dict[str, str] = {}
    for footprint in board.get_footprints():
        reference = footprint.reference_field.text.value
        names[footprint.id.value] = reference
        for pad in footprint.definition.pads:
            names[pad.id.value] = f"{reference}.{pad.number}"

    return [
        DrcViolation(
            type=DrcErrorType.Name(marker.error_type).removeprefix("DRCET_"),
            severity=RuleSeverity.Name(marker.severity).removeprefix("RS_"),
            description=marker.description,
            position=Vector2.from_xy(marker.position.x_nm, marker.position.y_nm),
            layer=layer_name(marker.layer),
            items=tuple(names.get(item.value, item.value) for item in marker.items),
            excluded=marker.excluded,
        )
        for marker in result.violations
    ]


def refill_zones(board: Board, *, timeout_s: float = 300.0) -> None:
    """Refill every zone on the board according to the current design rules.

    The zone filler takes its clearances from the DRC engine, and a headless server only loads
    the rules into that engine when a DRC runs. On a board created in this session, or after a
    net class changed, a plain ``board.refill_zones()`` therefore fills with missing or outdated
    clearances. Running a DRC first makes the fill honour the rules as they are now.
    """
    _start_drc(board, False, timeout_s)
    board.refill_zones()


def _start_drc(board: Board, schematic_parity: bool, timeout_s: float):
    start = board_tasks_pb2.StartDrcTask()
    start.board.CopyFrom(board.document)
    start.test_schematic_parity = schematic_parity
    return run_task(board.client, start, "DRC", timeout_s)


def _board_file(board: Board) -> Path:
    return Path(board.document.project.path) / board.name


def _angle_spans(content: str, item_ids) -> dict[str, re.Match]:
    """Where the saved board keeps the position and angle of each of these texts."""
    spans = {}
    for item_id in item_ids:
        end = content.find(f'(uuid "{item_id}")')
        if end < 0:
            continue
        start = max(content.rfind(opener, 0, end) for opener in _TEXT_OPENERS)
        match = _AT.search(content, start, end) if start >= 0 else None
        if match is not None:
            spans[item_id] = match
    return spans


def text_angles(board: Board, item_ids) -> dict[str, float]:
    """The angles, in degrees, that the saved board has for these texts (free texts and the
    texts and fields of footprints, by ID). See `set_text_angles` for why the file is asked."""
    board.save()
    spans = _angle_spans(_board_file(board).read_text(), item_ids)
    return {item_id: float(match[3] or 0) for item_id, match in spans.items()}


def set_text_angles(board: Board, angles: dict[str, float]) -> bool:
    """Turn board texts to the given angles in degrees, counter-clockwise as drawn.

    Setting a text's angle through the API changes what the API reports back, but not the angle
    KiCad draws, checks and saves: a board text keeps a second copy of its angle that the API
    never writes, and every update of the text or of its footprint through the API resets that
    copy. The saved file is the only way to it, so this saves the board, rewrites the angles
    there and has KiCad load the file again. Texts that already have their angle are left alone;
    returns whether anything had to be rewritten.
    """
    board.save()
    path = _board_file(board)
    content = path.read_text()
    edits = []
    for item_id, match in _angle_spans(content, angles).items():
        degrees = angles[item_id] % 360
        if (float(match[3] or 0) - degrees) % 360:
            edits.append((match.start(), match.end(), f"(at {match[1]} {match[2]} {degrees:g})"))
    for start, end, position in sorted(edits, reverse=True):
        content = content[:start] + position + content[end:]
    if edits:
        path.write_text(content)
        board.revert()
    return bool(edits)


def _footprint_text_ids(footprint) -> list[str]:
    texts = [item.text if isinstance(item, Field) else item for item in footprint.texts_and_fields]
    return list(dict.fromkeys(text.id.value for text in texts if isinstance(text, BoardText)))


def update_footprints(board: Board, footprints, *, turned=None, angles=None):
    """Update footprints through the API without losing the angles of their texts (see
    `set_text_angles`).

    ``turned`` maps a footprint ID to the degrees its rotation changes by, so that its texts turn
    with it; ``angles`` maps text IDs to angles asked for explicitly. Returns the footprints as
    they are afterwards.
    """
    footprints = list(footprints)
    turns = {
        item_id: (turned or {}).get(footprint.id.value, 0.0)
        for footprint in footprints
        for item_id in _footprint_text_ids(footprint)
    }
    wanted = {
        item_id: angle + turns[item_id] for item_id, angle in text_angles(board, turns).items()
    }
    wanted.update(angles or {})
    updated = board.update_items(footprints)
    if set_text_angles(board, wanted):
        updated = board.get_items_by_id([footprint.id for footprint in updated])
    return updated


def stackup_names(stackup) -> list[str]:
    """What the board file calls each layer of the stackup, top to bottom: the layer name, and
    ``dielectric N`` for what lies between two copper layers."""
    names, dielectrics = [], 0
    for layer in stackup.layers:
        if layer.type == BoardStackupLayerType.BSLT_DIELECTRIC:
            dielectrics += 1
            names.append(f"dielectric {dielectrics}")
        else:
            names.append(layer_name(layer.layer))
    return names


def _stackup_text(stackup) -> str:
    """The stackup KiCad derives for a board that has none of its own, as the board file words
    a stackup."""
    rows = []
    for name, layer in zip(stackup_names(stackup), stackup.layers):
        if layer.type == BoardStackupLayerType.BSLT_COPPER:
            row = f'(layer "{name}" (type "copper") (thickness {mm_text(layer.thickness)})'
        elif layer.type == BoardStackupLayerType.BSLT_DIELECTRIC:
            core = layer.dielectric.type == BoardStackupDielectricType.BSDT_CORE
            material = layer.dielectric.layers[0]
            row = (
                f'(layer "{name}" (type "{"core" if core else "prepreg"}") '
                f'(thickness {mm_text(layer.thickness)}) (material "{material.material_name}") '
                f"(epsilon_r {material.epsilon_r:g}) (loss_tangent {material.loss_tangent:g})"
            )
        else:
            side = "Top" if name.startswith("F.") else "Bottom"
            row = f'(layer "{name}" (type "{side} {_STACKUP_KINDS[layer.type]}")'
            if layer.type == BoardStackupLayerType.BSLT_SOLDERMASK:
                row += f" (thickness {mm_text(layer.thickness)})"
        rows.append(row + ")")
    return f'(stackup {" ".join(rows)} (copper_finish "None") (dielectric_constraints no))'


def _decimal(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _list_end(content: str, start: int) -> int:
    """Where the parenthesised list that opens at ``start`` ends."""
    depth, quoted, index = 0, False, start
    while True:
        char = content[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1


def _stackup_span(content: str) -> tuple[int, int] | None:
    """Where the saved board keeps its stackup, if it has one of its own."""
    setup = _SETUP.search(content)
    start = content.find("(stackup", setup.start(), _list_end(content, setup.start()))
    return None if start < 0 else (start, _list_end(content, start))


def set_stackup(board: Board, changes: dict[str, dict[str, float | str]]) -> None:
    """Change layers of the stackup: ``{"dielectric 1": {"type": "prepreg", "thickness": 0.2,
    "epsilon_r": 4.4}}``, by the names of `stackup_names`, a thickness in millimetres. The
    board thickness becomes the sum of the layers.

    KiCad declares an API command that writes the stackup but does not handle it yet, so, as
    for text angles, the saved board is rewritten and loaded again. A board has no stackup of
    its own until someone sets one - KiCad derives one from the layer count and the board
    thickness - so the derived one is written out first.
    """
    board.save()
    path = _board_file(board)
    content = path.read_text()
    span = _stackup_span(content)
    if span is None:
        start = _SETUP.search(content).end()
        content = f"{content[:start]} {_stackup_text(board.get_stackup())}{content[start:]}"
        span = _stackup_span(content)
    section = content[span[0] : span[1]]
    for name, values in changes.items():
        start = section.index(f'(layer "{name}"')
        end = _list_end(section, start)
        layer = section[start:end]
        for key, value in values.items():
            entry = f'({key} "{value}"' if isinstance(value, str) else f"({key} {_decimal(value)}"
            match = re.search(rf'\({key}\s+("[^"]*"|[^\s()]+)', layer)
            if match is None:
                layer = f"{layer[:-1]} {entry}))"
            else:
                layer = layer[: match.start()] + entry + layer[match.end() :]
        section = section[:start] + layer + section[end:]
    thickness = sum(float(x) for x in re.findall(r"\(thickness\s+([\d.]+)", section))
    content = content[: span[0]] + section + content[span[1] :]
    content = _BOARD_THICKNESS.sub(rf"\g<1>{_decimal(thickness)}", content, count=1)
    path.write_text(content)
    board.revert()


def reset_stackup(board: Board) -> bool:
    """Drop the board's own stackup, so that KiCad derives one again; returns whether there was
    one. A stackup does not follow a change of the layer count made through the API."""
    board.save()
    path = _board_file(board)
    content = path.read_text()
    span = _stackup_span(content)
    if span is None:
        return False
    path.write_text(content[: span[0]] + content[span[1] :])
    board.revert()
    return True


def pad_layer(pad) -> str | None:
    """The copper layer of a pad that is on one only, which a surface-mount pad is. None for a
    pad that goes through the board."""
    layers = [layer for layer in pad.padstack.layers if is_copper_layer(layer)]
    return layer_name(layers[0]) if len(layers) == 1 else None


def layer_name(layer) -> str:
    """A layer as KiCad shows it: ``BL_F_Cu`` -> ``"F.Cu"``. Empty for an undefined layer."""
    name = BoardLayer.Name(layer)
    if name in ("BL_UNKNOWN", "BL_UNDEFINED"):
        return ""
    return name.removeprefix("BL_").replace("_", ".")


def layer_from_name(name: str):
    """The reverse of :func:`layer_name`; raises ``ValueError`` for an unknown layer."""
    try:
        return BoardLayer.Value("BL_" + name.replace(".", "_"))
    except ValueError:
        raise ValueError(f"unknown layer {name!r}") from None


def format_mm(position: Vector2) -> str:
    """A coordinate as text, in millimetres: ``"120,100.825"``."""
    return f"{mm_text(position.x)},{mm_text(position.y)}"


def mm_text(nm: int | None) -> str:
    """A length in nanometres as millimetres text; ``"-"`` when it is not set."""
    if nm is None:
        return "-"
    return f"{nm / 1e6:.4f}".rstrip("0").rstrip(".")
