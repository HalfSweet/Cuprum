"""Schematic pieces that kicad-python does not provide yet: absolute pin positions, symbol
updates, a pin-level netlist and ERC.

Coordinates in text output are always in mils (the schematic grid is 50 mil).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kipy.geometry import Vector2
from kipy.proto.common.types import ElectricalPinType, KiCadObjectType, RuleSeverity
from kipy.proto.schematic import schematic_tasks_pb2, schematic_types_pb2
from kipy.proto.schematic.schematic_rules_pb2 import ErcErrorType
from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation
from kipy.schematic import Schematic
from kipy.schematic_types import SchematicSymbolInstance

from cuprum.tasks import run_task

# A local coordinate (x, y) rotates to (a*x + b*y, c*x + d*y). Sheet coordinates have Y pointing
# down. Derived by comparing against ERC marker positions in KiCad 10.99.
_ROTATION = {
    SchematicSymbolOrientation.SSO_0: (1, 0, 0, 1),
    SchematicSymbolOrientation.SSO_90: (0, 1, -1, 0),
    SchematicSymbolOrientation.SSO_180: (-1, 0, 0, -1),
    SchematicSymbolOrientation.SSO_270: (0, -1, 1, 0),
}


@dataclass(frozen=True)
class Pin:
    number: str
    name: str
    electrical_type: str
    """For example ``"input"``, ``"passive"`` or ``"power_input"``."""
    position: Vector2
    """The connection point, in absolute sheet coordinates."""


def symbol_pins(symbol: SchematicSymbolInstance) -> list[Pin]:
    """The pins of a placed symbol, with connection points in absolute sheet coordinates.

    KiCad reports pin positions in the symbol's local frame; this applies the symbol's position,
    rotation and mirroring. For a multi-unit symbol only the pins of the placed unit are returned.
    """
    a, b, c, d = _matrix(symbol)
    origin = symbol.position
    body_style = symbol.body_style or 1

    pins = []
    for child in symbol.definition.items:
        if child.kind != "SchematicPin":
            continue
        if child.unit not in (None, symbol.unit):
            continue
        if child.proto.HasField("body_style") and child.proto.body_style.style != body_style:
            continue
        pin = child.item
        local = pin.position
        pins.append(
            Pin(
                number=pin.number,
                name=pin.name,
                electrical_type=pin_type_name(pin.electrical_type),
                position=Vector2.from_xy(
                    origin.x + a * local.x + b * local.y,
                    origin.y + c * local.x + d * local.y,
                ),
            )
        )
    return pins


def pin_positions(symbol: SchematicSymbolInstance) -> dict[str, Vector2]:
    """Shorthand for :func:`symbol_pins`: pin number -> absolute connection point."""
    return {pin.number: pin.position for pin in symbol_pins(symbol)}


def pin_type_name(electrical_type) -> str:
    return ElectricalPinType.Name(electrical_type).removeprefix("EPT_").lower()


def update_symbol(sch: Schematic, symbol: SchematicSymbolInstance) -> SchematicSymbolInstance:
    """Write changes to a placed symbol (position, orientation, value, footprint, ...) back to
    KiCad and return the updated instance.

    This differs from plain ``sch.update_items(symbol)`` in two ways. The instance is sent without
    its symbol definition; otherwise KiCad rewrites the library symbol embedded in the schematic
    from it and ERC then reports that the symbol differs from its library. And when the symbol
    was moved, rotated or mirrored, its field texts (reference, value, ...) are carried along;
    KiCad stores them in absolute coordinates and would leave them behind.
    """
    proto = schematic_types_pb2.SchematicSymbolInstance()
    proto.CopyFrom(symbol.proto)
    proto.ClearField("definition")
    updated = SchematicSymbolInstance(proto=proto)
    _carry_fields(updated, sch.get_items_by_id(symbol.id)[0])
    return sch.update_items(updated)[0]


def _carry_fields(new: SchematicSymbolInstance, old: SchematicSymbolInstance) -> None:
    """Move the field texts of ``new`` to where they sit relative to the body in ``old``.

    A field whose position the caller changed explicitly is left alone.
    """
    old_matrix, new_matrix = _matrix(old), _matrix(new)
    old_origin, new_origin = old.position, new.position
    if old_matrix == new_matrix and (old_origin.x, old_origin.y) == (new_origin.x, new_origin.y):
        return

    oa, ob, oc, od = old_matrix
    na, nb, nc, nd = new_matrix
    old_positions = {field.name: field.text.position for field in _fields(old)}
    for field in _fields(new):
        before = old_positions.get(field.name)
        position = field.text.position
        if before is None or (before.x, before.y) != (position.x, position.y):
            continue
        # Back into the symbol's local frame (the matrices are orthogonal, so the inverse is the
        # transpose), then out again through the new transform.
        dx, dy = before.x - old_origin.x, before.y - old_origin.y
        local_x, local_y = oa * dx + oc * dy, ob * dx + od * dy
        field.text.position = Vector2.from_xy(
            new_origin.x + na * local_x + nb * local_y,
            new_origin.y + nc * local_x + nd * local_y,
        )


def _fields(symbol: SchematicSymbolInstance):
    return [
        symbol.reference_field,
        symbol.value_field,
        symbol.footprint_field,
        symbol.datasheet_field,
        symbol.description_field,
        *symbol.user_fields,
    ]


def _matrix(symbol: SchematicSymbolInstance) -> tuple[int, int, int, int]:
    """The symbol's rotation and mirroring as (a, b, c, d): a local (x, y) maps to
    (a*x + b*y, c*x + d*y) relative to the symbol origin."""
    transform = symbol.transform
    a, b, c, d = _ROTATION.get(transform.orientation, _ROTATION[SchematicSymbolOrientation.SSO_0])
    # Rotate first, then mirror: mirror_x flips across the X axis (negates y), mirror_y flips
    # across the Y axis (negates x).
    sign_x = -1 if transform.mirror_y else 1
    sign_y = -1 if transform.mirror_x else 1
    return sign_x * a, sign_x * b, sign_y * c, sign_y * d


def netlist(sch: Schematic) -> dict[str, list[str]]:
    """Export the pin-level connectivity of the schematic: net name -> pins such as ``"R1.2"``.

    The connectivity comes from KiCad's own engine; this only replaces the object IDs in its
    answer with references and pin numbers. Power symbols (references starting with ``#``) are
    left out, since the net name already reflects them.
    """
    pins, _symbols = _owners(sch)
    nets: dict[str, list[str]] = {}
    for net in sch.get_netlist(types=KiCadObjectType.KOT_SCH_PIN):
        members = []
        for sheet in net.sheets:
            sheet_key = _path_key(sheet.path.path)
            for item in sheet.items:
                name = pins.get((sheet_key, item.value))
                if name is not None and not name.startswith("#"):
                    members.append(name)
        nets[net.name] = sorted(members, key=_natural)
    return nets


def netlist_text(sch: Schematic) -> str:
    """:func:`netlist` as plain text, one net per line: ``/VOUT: C2.1 R1.1 U1.5``."""
    nets = netlist(sch)
    return "\n".join(f"{name}: {' '.join(nets[name])}" for name in sorted(nets, key=_natural))


@dataclass(frozen=True)
class ErcViolation:
    type: str
    """For example ``"PIN_NOT_CONNECTED"``."""
    severity: str
    """For example ``"ERROR"`` or ``"WARNING"``."""
    description: str
    position: Vector2
    items: tuple[str, ...]
    """The objects involved: pins as ``"R1.2"``, symbols by reference, anything else by ID."""
    excluded: bool

    def __str__(self) -> str:
        items = f" [{', '.join(self.items)}]" if self.items else ""
        excluded = " (excluded)" if self.excluded else ""
        return (
            f"{self.severity} {self.type} @{format_mil(self.position)}: "
            f"{self.description}{items}{excluded}"
        )


def run_erc(sch: Schematic, timeout_s: float = 120.0) -> list[ErcViolation]:
    """Run ERC on the whole schematic and return the violations."""
    client = sch.client
    start = schematic_tasks_pb2.StartErcTask()
    start.document.CopyFrom(sch.document)
    token = run_task(client, start, "ERC", timeout_s)

    request = schematic_tasks_pb2.GetErcTaskResult()
    request.token.CopyFrom(token)
    result = client.send(request, schematic_tasks_pb2.ErcTaskResult)

    pins, symbols = _owners(sch)
    violations = []
    for marker in result.violations:
        item_paths = [
            marker.main_item_sheet_path if marker.HasField("main_item_sheet_path") else None,
            marker.aux_item_sheet_path if marker.HasField("aux_item_sheet_path") else None,
        ]
        items = []
        for index, item in enumerate(marker.items):
            path = item_paths[index] if index < len(item_paths) else None
            key = (_path_key(path.path), item.value) if path is not None else None
            items.append(pins.get(key) or symbols.get(key) or item.value)
        violations.append(
            ErcViolation(
                type=ErcErrorType.Name(marker.error_type).removeprefix("ERCET_"),
                severity=RuleSeverity.Name(marker.severity).removeprefix("RS_"),
                description=marker.description,
                position=Vector2.from_xy(marker.position.x_nm, marker.position.y_nm),
                items=tuple(items),
                excluded=marker.excluded,
            )
        )
    return violations


def _owners(sch: Schematic):
    """Two tables keyed by (sheet path, object ID): pins -> ``"R1.2"`` and symbols -> ``"R1"``.
    Sheet symbols and their pins are named the same way, by the sheet's name.

    When a sub-sheet is used more than once, its instances share object IDs but have different
    references, so the sheet path has to be part of the key.
    """
    pins: dict[tuple, str] = {}
    symbols: dict[tuple, str] = {}
    for symbol in sch.get_symbols():
        sheet_key = _path_key(symbol.path.path)
        symbols[(sheet_key, symbol.id.value)] = symbol.reference
        for pin in symbol.definition.pins:
            pins[(sheet_key, pin.id.value)] = f"{symbol.reference}.{pin.number}"
    for sheet in sch.get_sheet_symbols():
        sheet_key = _path_key(sheet.path.path)
        name = sheet.name_field.text.value
        symbols[(sheet_key, sheet.id.value)] = name
        for pin in sheet.pins:
            pins[(sheet_key, pin.id.value)] = f"{name}.{pin.text.value}"
    return pins, symbols


def format_mil(position: Vector2) -> str:
    """A coordinate as text, in mils: ``"4000,3000"``."""
    return f"{_mil(position.x)},{_mil(position.y)}"


def _mil(nm: int) -> str:
    return f"{nm / 25_400:.3f}".rstrip("0").rstrip(".")


def _path_key(path) -> tuple[str, ...]:
    return tuple(kiid.value for kiid in path)


def _natural(text: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]
