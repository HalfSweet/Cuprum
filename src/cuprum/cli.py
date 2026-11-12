"""The cuprum command line.

Each command attaches to the project's background KiCad server (starting one if needed), does one
thing and prints the result. Commands that change the design save it right away, so the files on
disk are always current. Schematic coordinates are in mils; the schematic grid is 50 mil.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import contextmanager
from functools import cache
from itertools import pairwise
from pathlib import Path

from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.geometry import Vector2
from kipy.proto.common.commands import editor_commands_pb2, library_commands_pb2
from kipy.proto.common.types import (
    HorizontalAlignment,
    LibraryType,
    PageOrientation,
    PageSize,
    VerticalAlignment,
)
from kipy.proto.schematic import schematic_types_pb2 as pb
from kipy.schematic_types import (
    GlobalLabel,
    HierarchicalLabel,
    Junction,
    LocalLabel,
    NoConnectMarker,
    SchematicField,
    SchematicLine,
    SchematicSymbol,
    SchematicText,
    SheetPin,
    SheetSymbol,
)
from kipy.util import unpack_any

from cuprum import cli_lib, cli_pcb
from cuprum.cli_common import (
    JSON_HELP,
    CliError,
    area_data,
    area_row,
    emit,
    mil,
    natural,
    num,
    number,
    render_directory,
    resolve_schematic,
    section_lines,
    symbol_definition_data,
    symbol_definition_lines,
    violation_data,
    violation_lines,
    xy,
)
from cuprum.export import kicad_cli, render_schematic, render_schematic_area
from cuprum.schematic import netlist, run_erc, symbol_pins, update_symbol
from cuprum.session import connect, stop

MIL = 25_400  # nm

_ORIENTATIONS = {0: pb.SSO_0, 90: pb.SSO_90, 180: pb.SSO_180, 270: pb.SSO_270}
_DEGREES = {value: degrees for degrees, value in _ORIENTATIONS.items()}
_SPINS = {"left": pb.SLSS_LEFT, "right": pb.SLSS_RIGHT, "up": pb.SLSS_UP, "down": pb.SLSS_BOTTOM}
_SPIN_NAMES = {value: name for name, value in _SPINS.items()}
_SHAPES = {
    "input": pb.SLSH_INPUT,
    "output": pb.SLSH_OUTPUT,
    "bidi": pb.SLSH_BIDI,
    "tristate": pb.SLSH_TRISTATE,
    "passive": pb.SLSH_PASSIVE,
}
_SHAPE_NAMES = {value: name for name, value in _SHAPES.items()}
_LABEL_KINDS = {LocalLabel: "local", GlobalLabel: "global", HierarchicalLabel: "hier"}
_PAGE_SIZES = {
    name.removeprefix("PS_").replace("ANSI_", "").replace("US_", "US"): value
    for name, value in PageSize.items()
    if name not in ("PS_UNKNOWN", "PS_USER", "PS_GERBER")
}
_PAGE_SIZE_NAMES = {value: name for name, value in _PAGE_SIZES.items()}

_XY = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
# Option of 'set' -> the attribute of the symbol it controls, and the option value (--dnp is
# True, --no-bom is False) that switches the attribute on.
_SYMBOL_FLAGS = {
    "dnp": ("do_not_populate", True),
    "bom": ("exclude_from_bill_of_materials", False),
    "board": ("exclude_from_board", False),
    "sim": ("exclude_from_simulation", False),
    "pos": ("exclude_from_position_files", False),
}
_FLAG_NAMES = {"dnp": "dnp", "bom": "no-bom", "board": "no-board", "sim": "no-sim", "pos": "no-pos"}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        args.handler(args)
    except (CliError, ApiError, KiCadConnectionError, RuntimeError, FileNotFoundError) as error:
        print(f"cuprum: {error}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Connecting and parsing


@contextmanager
def _schematic(args):
    """Yield (kicad, sheet): the connection and the schematic bound to the sheet the command
    works on, which is the root sheet unless --sheet names another."""
    path = resolve_schematic(args.file)
    if not path.exists():
        raise CliError(f"{path} does not exist; create it with 'cuprum new'")
    with connect(path) as kicad:
        schematic = kicad.get_schematic()
        sheet = _find_sheet(schematic.get_hierarchy(), getattr(args, "sheet", None))
        yield kicad, schematic.for_sheet(sheet)


def _find_sheet(hierarchy, wanted: str | None):
    if wanted is None:
        return hierarchy.root
    sheets = list(hierarchy)
    by_path = [s for s in sheets if s.path.path_human_readable.strip("/") == wanted.strip("/")]
    matches = by_path or [s for s in sheets if s.name == wanted]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(s.path.path_human_readable for s in sheets)
    problem = "matches several sheets" if matches else "is not a sheet"
    raise CliError(f"{wanted} {problem}; the sheets are {known}")


def _save(kicad) -> None:
    kicad.get_schematic().save()


@contextmanager
def _library(args):
    """For library-only commands: use the project's server if there is a project, otherwise a
    server that is not bound to any project."""
    path = resolve_schematic(args.file, required=False)
    with connect(path if path is not None and path.exists() else None) as kicad:
        yield kicad


def _xy(token: str) -> Vector2 | None:
    match = _XY.fullmatch(token)
    if match:
        return Vector2.from_xy(round(float(match[1]) * MIL), round(float(match[2]) * MIL))
    return None


def _point_parser(sch):
    """Return a function that turns ``X,Y`` or ``REF.PIN`` into a coordinate. REF is a symbol
    reference or the name of a sheet symbol. The sheet is only read once a pin is referenced."""
    symbols = cache(sch.get_symbols)
    sheets = cache(sch.get_sheet_symbols)

    def parse(token: str) -> Vector2:
        point = _xy(token)
        if point is not None:
            return point
        reference, dot, pin = token.partition(".")
        if not (dot and pin):
            raise CliError(f"bad position {token!r}: expected X,Y in mils or REF.PIN such as R1.2")
        if not any(s.reference == reference for s in symbols()):
            for sheet in sheets():
                if sheet.name_field.text.value == reference:
                    return _find_sheet_pin(sheet, pin).position
        return _find_pin(symbols(), reference, pin).position

    return parse


def _find_pin(symbols, reference: str, pin: str):
    pins = [p for s in _instances(symbols, reference) for p in symbol_pins(s)]
    by_number = [p for p in pins if p.number == pin]
    if by_number:
        return by_number[0]
    by_name = [p for p in pins if p.name == pin]
    if len(by_name) == 1:
        return by_name[0]
    if by_name:
        numbers = ", ".join(p.number for p in by_name)
        raise CliError(f"{reference} has several pins named {pin} ({numbers}); use the pin number")
    known = ", ".join(sorted((p.number for p in pins), key=natural))
    raise CliError(f"{reference} has no pin {pin}; its pins are {known}")


def _find_sheet_pin(sheet, name: str):
    for pin in sheet.pins:
        if pin.text.value == name:
            return pin
    known = ", ".join(pin.text.value for pin in sheet.pins) or "none"
    raise CliError(f"sheet {sheet.name_field.text.value} has no pin {name}; its pins are {known}")


def _find_sheet_symbol(sch, name: str):
    for sheet in sch.get_sheet_symbols():
        if sheet.name_field.text.value == name:
            return sheet
    raise CliError(f"no sheet symbol named {name} on this sheet")


def _instances(symbols, reference: str):
    instances = [s for s in symbols if s.reference == reference]
    if not instances:
        raise CliError(f"no symbol with reference {reference}")
    return instances


def _transform(symbol) -> tuple[int, str | None]:
    transform = symbol.transform
    mirror = "x" if transform.mirror_x else "y" if transform.mirror_y else None
    return _DEGREES.get(transform.orientation, 0), mirror


def _set_transform(symbol, rotation: int, mirror: str | None) -> None:
    # Rotating by 180 and mirroring equals mirroring across the other axis without rotating.
    # KiCad reads transforms back in the latter form, and it drops the mirror when it is sent
    # 180 + mirror_x directly.
    if rotation == 180 and mirror:
        rotation, mirror = 0, "y" if mirror == "x" else "x"
    symbol.transform.orientation = _ORIENTATIONS[rotation]
    symbol.transform.mirror_x = mirror == "x"
    symbol.transform.mirror_y = mirror == "y"


def _set_fields(symbol, assignments: list[str]) -> None:
    """Apply NAME=VALUE assignments to the symbol's own fields; an empty value drops the field."""
    fields = list(symbol.user_fields)
    for assignment in assignments:
        name, equals, value = assignment.partition("=")
        if not (name and equals):
            raise CliError(f"bad field {assignment!r}: expected NAME=VALUE")
        existing = next((f for f in fields if f.name == name), None)
        if not value:
            fields = [f for f in fields if f.name != name]
        elif existing is not None:
            existing.text.value = value
        else:
            field = SchematicField()
            field.name = name
            field.text.value = value
            field.text.position = symbol.position
            field.visible = False
            fields.append(field)
    symbol.user_fields = fields


def _next_reference(symbols, prefix: str) -> str:
    pattern = re.compile(re.escape(prefix) + r"(\d+)")
    used = [int(m[1]) for s in symbols if (m := pattern.fullmatch(s.reference))]
    number = max(used, default=0) + 1
    return f"{prefix}{number:02d}" if prefix.startswith("#") else f"{prefix}{number}"


# ---------------------------------------------------------------------------
# Output: what is on a sheet as data, in mils, and that data as text


def _at(position) -> dict:
    return {"x": mil(position.x), "y": mil(position.y)}


def _symbol_data(symbol, *, detail: bool = False) -> dict:
    """A placed symbol; with ``detail`` also its fields, its texts and where its pins are."""
    lib_id = symbol.definition.id
    rotation, mirror = _transform(symbol)
    data = {
        "reference": symbol.reference,
        "lib_id": f"{lib_id.library}:{lib_id.name}",
        "value": symbol.value or None,
        "position": _at(symbol.position),
        "rotation": rotation,
        "mirror": mirror,
        "unit": _unit(symbol),
        "footprint": symbol.footprint_field.text.value or None,
        "dnp": symbol.attributes.do_not_populate,
        "excluded_from": [
            option
            for option, (attribute, _value) in _SYMBOL_FLAGS.items()
            if option != "dnp" and getattr(symbol.attributes, attribute)
        ],
    }
    if detail:
        data["fields"] = {field.name: field.text.value for field in symbol.user_fields}
        data["texts"] = [
            _field_data(getattr(symbol, f"{name}_field"), name) for name in ("reference", "value")
        ]
        data["pins"] = [
            {
                "number": pin.number,
                "name": pin.name if pin.name not in ("", "~") else None,
                "type": pin.electrical_type,
                "position": _at(pin.position),
            }
            for pin in sorted(symbol_pins(symbol), key=lambda p: natural(p.number))
        ]
    return data


def _unit(symbol) -> int | None:
    """Which unit of a multi-unit device the symbol is; None for a device of one unit."""
    return symbol.unit if symbol.definition.unit_count > 1 else None


def _symbol_lines(data: dict, *, detail: bool = False) -> list[str]:
    parts = [data["reference"], data["lib_id"]]
    if data["value"]:
        parts.append(data["value"])
    parts.append("@" + xy(data["position"]))
    if data["rotation"]:
        parts.append(f"rot{data['rotation']}")
    if data["mirror"]:
        parts.append(f"mirror-{data['mirror']}")
    if data["unit"] is not None:
        parts.append(f"unit{data['unit']}")
    if data["footprint"]:
        parts.append(f"fp={data['footprint']}")
    if data["dnp"]:
        parts.append("dnp")
    parts += [f"no-{name}" for name in data["excluded_from"]]
    lines = ["  ".join(parts)]
    if detail:
        fields = [f"{name}={value}" for name, value in data["fields"].items()]
        if fields:
            lines.append("  fields: " + "  ".join(fields))
        lines += ["  " + _field_row(text) for text in data["texts"]]
        for pin in data["pins"]:
            lines.append(
                f"  {pin['number']:<4} {pin['name'] or '~':<12} {pin['type']:<14} "
                f"@{xy(pin['position'])}"
            )
    return lines


def _symbol_listing(*, detail: bool):
    """The text of a command that answers with symbols."""
    return lambda data: [
        line for symbol in data["symbols"] for line in _symbol_lines(symbol, detail=detail)
    ]


def _field_data(field, name: str) -> dict:
    return {"name": name, "position": _at(field.text.position), "visible": field.visible}


def _field_row(data: dict) -> str:
    hidden = "" if data["visible"] else "  hidden"
    return f"{data['name']} text  @{xy(data['position'])}{hidden}"


def _line_data(line) -> dict:
    return {"id": line.id.value, "start": _at(line.start), "end": _at(line.end)}


def _line_row(data: dict) -> str:
    return f"{data['id'][:8]}  {xy(data['start'])} -> {xy(data['end'])}"


def _label_data(label) -> dict:
    shaped = isinstance(label, (GlobalLabel, HierarchicalLabel))
    return {
        "id": label.id.value,
        "kind": _LABEL_KINDS.get(type(label), "directive"),
        "text": label.text.value,
        "position": _at(label.position),
        "spin": _SPIN_NAMES.get(label.spin_style),
        "shape": _SHAPE_NAMES.get(label.shape) if shaped else None,
    }


def _label_row(data: dict) -> str:
    position = xy(data["position"])
    return (
        f"{data['id'][:8]}  {data['kind']} {data['text']}  @{position}  {data['spin'] or ''}"
    ).rstrip()


def _marker_data(item) -> dict:
    return {"id": item.id.value, "position": _at(item.position)}


def _marker_row(data: dict) -> str:
    return f"{data['id'][:8]}  @{xy(data['position'])}"


def _sheet_data(sheet) -> dict:
    return {
        "id": sheet.id.value,
        "name": sheet.name_field.text.value,
        "file": sheet.filename_field.text.value,
        "position": _at(sheet.position),
        "width": mil(sheet.size.x),
        "height": mil(sheet.size.y),
        "pins": [
            {
                "name": pin.text.value,
                "shape": _SHAPE_NAMES.get(pin.shape),
                "position": _at(pin.position),
            }
            for pin in sheet.pins
        ],
    }


def _sheet_lines(data: dict) -> list[str]:
    size = f"{num(data['width'])}x{num(data['height'])}"
    lines = [f"{data['id'][:8]}  {data['name']}  {data['file']}  @{xy(data['position'])}  {size}"]
    for pin in data["pins"]:
        lines.append(f"  {pin['name']}  {pin['shape'] or ''}  @{xy(pin['position'])}".rstrip())
    return lines


def _new_sheet_lines(data: dict) -> list[str]:
    lines = [line for sheet in data["sheet_symbols"] for line in _sheet_lines(sheet)]
    return ["sheet " + lines[0], *lines[1:]]


def _note_data(text) -> dict:
    return {"id": text.id.value, "position": _at(text.position), "text": text.value}


def _note_row(data: dict) -> str:
    return f"{data['id'][:8]}  @{xy(data['position'])}  {data['text']!r}"


def _sheet_contents(data: dict) -> list[str]:
    sections = {
        "symbols": [_symbol_lines(symbol)[0] for symbol in data["symbols"]],
        "sheets": [row for sheet in data["sheet_symbols"] for row in _sheet_lines(sheet)],
        "wires": [_line_row(wire) for wire in data["wires"]],
        "buses": [_line_row(bus) for bus in data["buses"]],
        "junctions": [_marker_row(junction) for junction in data["junctions"]],
        "labels": [_label_row(label) for label in data["labels"]],
        "no-connects": [_marker_row(marker) for marker in data["no_connects"]],
        "text": [_note_row(note) for note in data["texts"]],
    }
    return section_lines(sections) or ["(empty)"]


# ---------------------------------------------------------------------------
# Commands


def _cmd_new(args):
    path = Path(args.path).expanduser().resolve()
    if path.suffix != ".kicad_sch":
        raise CliError("the file name must end in .kicad_sch")
    if path.exists():
        raise CliError(f"{path} already exists")
    with connect(path) as kicad:
        _save(kicad)
    emit(args, {"created": str(path)}, lambda data: [f"created {data['created']}"])


def _cmd_show(args):
    with _schematic(args) as (_kicad, sch):
        symbols = sorted(sch.get_symbols(), key=lambda s: (natural(s.reference), s.unit))
        if args.references:
            chosen = [s for reference in args.references for s in _instances(symbols, reference)]
            data = {"symbols": [_symbol_data(symbol, detail=True) for symbol in chosen]}
            emit(args, data, _symbol_listing(detail=True))
            return

        lines = sch.get_lines()

        def lines_of(kind) -> list[dict]:
            return sorted((_line_data(w) for w in lines if w.type == kind), key=_line_row)

        data = {
            "symbols": [_symbol_data(symbol) for symbol in symbols],
            "sheet_symbols": [_sheet_data(sheet) for sheet in sch.get_sheet_symbols()],
            "wires": lines_of(pb.SLT_WIRE),
            "buses": lines_of(pb.SLT_BUS),
            "junctions": [_marker_data(junction) for junction in sch.get_junctions()],
            "labels": [_label_data(label) for label in sch.get_labels()],
            "no_connects": [_marker_data(marker) for marker in sch.get_no_connects()],
            "texts": [_note_data(t) for t in sch.get_text() if isinstance(t, SchematicText)],
        }
        emit(args, data, _sheet_contents)


def _cmd_sheets(args):
    root_file = resolve_schematic(args.file).name
    with _schematic(args) as (kicad, _sch):
        sheets = [
            {
                "path": sheet.path.path_human_readable,
                "file": sheet.filename or root_file,
                "page": sheet.page_number,
            }
            for sheet in kicad.get_schematic().get_hierarchy()
        ]
    emit(
        args,
        {"sheets": sheets},
        lambda data: [f"{s['path']:<24} {s['file']}  page {s['page']}" for s in data["sheets"]],
    )


def _cmd_sheet_add(args):
    with _schematic(args) as (kicad, sch):
        size = _xy(args.size)
        if size is None:
            raise CliError(f"bad size {args.size!r}: expected WIDTH,HEIGHT in mils")
        sheet = SheetSymbol()
        sheet.position = _point_parser(sch)(args.at)
        sheet.size = size
        sheet.name_field.text.value = args.name
        sheet.filename_field.text.value = args.file_name or f"{args.name}.kicad_sch"
        created = sch.create_items(sheet)
        _save(kicad)
        emit(args, {"sheet_symbols": [_sheet_data(created[0])]}, _new_sheet_lines)


def _cmd_sheet_pin(args):
    with _schematic(args) as (kicad, sch):
        sheet = _find_sheet_symbol(sch, args.sheet_name)
        wanted = _point_parser(sch)(args.at)
        left, top = sheet.position.x, sheet.position.y
        right, bottom = left + sheet.size.x, top + sheet.size.y
        # A sheet pin sits on the border: move the point onto the nearest edge.
        x, y = min(max(wanted.x, left), right), min(max(wanted.y, top), bottom)
        side, x, y = min(
            (
                (abs(x - left), pb.SHS_LEFT, left, y),
                (abs(x - right), pb.SHS_RIGHT, right, y),
                (abs(y - top), pb.SHS_TOP, x, top),
                (abs(y - bottom), pb.SHS_BOTTOM, x, bottom),
            )
        )[1:]
        pin = SheetPin()
        pin.text.value = args.name
        pin.position = Vector2.from_xy(x, y)
        pin.side = side
        pin.shape = _SHAPES[args.shape]
        sheet.pins.append(pin)
        updated = sch.update_items(sheet)
        _save(kicad)
        emit(args, {"sheet_symbols": [_sheet_data(updated[0])]}, _new_sheet_lines)


def _cmd_add(args):
    with _schematic(args) as (kicad, sch):
        definition = kicad.get_library_symbol(args.lib_id)
        if args.unit is not None and args.ref is None:
            raise CliError("--unit needs --ref to say which device the unit belongs to")
        # References have to be unique across every sheet, not just this one.
        reference = args.ref or _next_reference(
            kicad.get_schematic().get_symbols(), definition.reference_field.text.value
        )
        symbol = sch.place_symbol_from_library(
            args.lib_id,
            _point_parser(sch)(args.at),
            orientation=_ORIENTATIONS[args.rot],
            unit=args.unit,
            reference=reference,
        )
        if args.mirror or args.value is not None or args.footprint is not None or args.field:
            if args.mirror:
                _set_transform(symbol, args.rot, args.mirror)
            if args.value is not None:
                symbol.value = args.value
            if args.footprint is not None:
                symbol.footprint_field.text.value = args.footprint
            _set_fields(symbol, args.field)
            symbol = update_symbol(sch, symbol)
        _save(kicad)
        emit(args, {"symbols": [_symbol_data(symbol, detail=True)]}, _symbol_listing(detail=True))


def _cmd_set(args):
    with _schematic(args) as (kicad, sch):
        instances = _instances(sch.get_symbols(), args.reference)
        if args.unit is not None:
            instances = [s for s in instances if s.unit == args.unit]
            if not instances:
                raise CliError(f"unit {args.unit} of {args.reference} is not placed")
        moving = args.at is not None or args.rot is not None or args.mirror is not None
        if moving and len(instances) > 1:
            raise CliError(f"{args.reference} has several units placed; pick one with --unit")

        updated = []
        for symbol in instances:
            if args.value is not None:
                symbol.value = args.value
            if args.footprint is not None:
                symbol.footprint_field.text.value = args.footprint
            if args.ref is not None:
                symbol.reference = args.ref
            if args.at is not None:
                symbol.position = _point_parser(sch)(args.at)
            if args.rot is not None or args.mirror is not None:
                rotation, mirror = _transform(symbol)
                if args.rot is not None:
                    rotation = args.rot
                if args.mirror is not None:
                    mirror = None if args.mirror == "none" else args.mirror
                _set_transform(symbol, rotation, mirror)
            _set_fields(symbol, args.field)
            for option, (attribute, value) in _SYMBOL_FLAGS.items():
                if getattr(args, option) is not None:
                    setattr(symbol.attributes, attribute, getattr(args, option) == value)
            updated.append(update_symbol(sch, symbol))
        _save(kicad)
        # The text only spells a symbol out when its pins moved or its fields changed.
        data = {"symbols": [_symbol_data(symbol, detail=True) for symbol in updated]}
        emit(args, data, _symbol_listing(detail=moving or bool(args.field)))


def _cmd_field(args):
    if args.at is None and args.visible is None:
        raise CliError("nothing to do: give a position, --hide or --show")
    with _schematic(args) as (kicad, sch):
        instances = _instances(sch.get_symbols(), args.reference)
        if args.unit is not None:
            instances = [s for s in instances if s.unit == args.unit]
            if not instances:
                raise CliError(f"unit {args.unit} of {args.reference} is not placed")
        if args.at is not None and len(instances) > 1:
            raise CliError(f"{args.reference} has several units placed; pick one with --unit")
        rows = []
        for symbol in instances:
            fields = {
                "reference": symbol.reference_field,
                "value": symbol.value_field,
                "footprint": symbol.footprint_field,
                **{field.name: field for field in symbol.user_fields},
            }
            field = fields.get(args.name)
            if field is None:
                raise CliError(
                    f"{args.reference} has no field {args.name}; it has {', '.join(fields)}"
                )
            if args.at is not None:
                field.text.position = _point_parser(sch)(args.at)
            if args.visible is not None:
                field.visible = args.visible
            update_symbol(sch, symbol)
            rows.append(
                {
                    "reference": args.reference,
                    "unit": _unit(symbol),
                    **_field_data(field, args.name),
                }
            )
        _save(kicad)
        emit(
            args,
            {"fields": rows},
            lambda data: [f"{row['reference']} {_field_row(row)}" for row in data["fields"]],
        )


def _cmd_wire(args):
    with _schematic(args) as (kicad, sch):
        parse = _point_parser(sch)
        points = [parse(token) for token in args.points]
        segments = []
        for start, end in pairwise(points):
            if (start.x, start.y) == (end.x, end.y):
                continue
            segment = SchematicLine()
            segment.start, segment.end, segment.type = start, end, pb.SLT_WIRE
            segments.append(segment)
        if not segments:
            raise CliError("need at least two distinct points")
        created = sch.create_items(segments)
        _save(kicad)
        emit(
            args,
            {"wires": [_line_data(line) for line in created]},
            lambda data: [f"wire {_line_row(wire)}" for wire in data["wires"]],
        )


def _cmd_label(args):
    with _schematic(args) as (kicad, sch):
        label = (
            GlobalLabel() if args.is_global else HierarchicalLabel() if args.hier else LocalLabel()
        )
        label.position = _point_parser(sch)(args.at)
        label.text.value = args.text
        label.spin_style = _SPINS[args.spin]
        if args.shape is not None:
            if isinstance(label, LocalLabel):
                raise CliError("--shape only applies to global and hierarchical labels")
            label.shape = _SHAPES[args.shape]
        created = sch.create_items(label)
        _save(kicad)
        emit(args, {"labels": [_label_data(created[0])]}, _label_and_note_lines)


def _label_and_note_lines(data: dict) -> list[str]:
    labels = [f"label {_label_row(label)}" for label in data.get("labels", [])]
    return labels + [f"text {_note_row(note)}" for note in data.get("texts", [])]


def _marker_command(factory, name, key):
    def command(args):
        with _schematic(args) as (kicad, sch):
            parse = _point_parser(sch)
            items = []
            for token in args.points:
                item = factory()
                item.position = parse(token)
                items.append(item)
            created = sch.create_items(items)
            _save(kicad)
            emit(
                args,
                {key: [_marker_data(item) for item in created]},
                lambda data: [f"{name} {_marker_row(marker)}" for marker in data[key]],
            )

    return command


def _cmd_text(args):
    with _schematic(args) as (kicad, sch):
        text = SchematicText()
        text.position = _point_parser(sch)(args.at)
        text.value = args.text
        # Let KiCad fill in its default size and style first: attributes sent with a new text
        # replace all of them, and an unset size makes the text vanish. Then anchor the text at
        # its bottom-left corner, the way a note typed in the editor is.
        text = sch.create_items(text)[0]
        text.text.attributes.horizontal_alignment = HorizontalAlignment.HA_LEFT
        text.text.attributes.vertical_alignment = VerticalAlignment.VA_BOTTOM
        created = sch.update_items(text)
        _save(kicad)
        emit(args, {"texts": [_note_data(created[0])]}, _label_and_note_lines)


def _cmd_edit(args):
    if args.at is None and args.text is None and args.spin is None:
        raise CliError("nothing to do: give --at, --text or --spin")
    with _schematic(args) as (kicad, sch):
        labels, texts = sch.get_labels(), sch.get_text()
        matches = [item for item in [*labels, *texts] if item.id.value.startswith(args.target)]
        if len(matches) != 1:
            problem = "matches several objects" if matches else "is not the ID of a label or text"
            raise CliError(f"{args.target} {problem}")
        item = matches[0]
        is_label = any(item is label for label in labels)
        if args.at is not None:
            item.position = _point_parser(sch)(args.at)
        if args.text is not None:
            if is_label:
                item.text.value = args.text
            else:
                item.value = args.text
        if args.spin is not None:
            if not is_label:
                raise CliError("--spin only applies to labels")
            item.spin_style = _SPINS[args.spin]
        updated = sch.update_items(item)[0]
        _save(kicad)
        data = {
            "labels": [_label_data(updated)] if is_label else [],
            "texts": [] if is_label else [_note_data(updated)],
        }
        emit(args, data, _label_and_note_lines)


def _cmd_rm(args):
    with _schematic(args) as (kicad, sch):
        symbols = sch.get_symbols()

        @cache
        def others():
            lines = [("wire" if w.type == pb.SLT_WIRE else "line", w) for w in sch.get_lines()]
            return (
                lines
                + [("label", item) for item in sch.get_labels()]
                + [("junction", item) for item in sch.get_junctions()]
                + [("nc", item) for item in sch.get_no_connects()]
                + [("sheet", item) for item in sch.get_sheet_symbols()]
                + [("text", item) for item in sch.get_text()]
            )

        doomed, removed = [], []
        for target in args.targets:
            by_reference = [s for s in symbols if s.reference == target]
            if by_reference:
                doomed += by_reference
                removed.append({"kind": "symbol", "reference": target, "id": None})
                continue
            matches = [(kind, item) for kind, item in others() if item.id.value.startswith(target)]
            if len(matches) != 1:
                problem = (
                    "matches several objects" if matches else "is neither a reference nor an ID"
                )
                raise CliError(
                    f"{target} {problem}; nothing was removed. KiCad joins wires that meet end "
                    "to end, which gives them new IDs; 'cuprum show' lists the current ones"
                )
            kind, item = matches[0]
            doomed.append(item)
            removed.append({"kind": kind, "reference": None, "id": item.id.value})
        sch.remove_items(doomed)
        _save(kicad)
        emit(
            args,
            {"removed": removed},
            lambda data: [
                f"removed {row['reference'] or row['kind'] + ' ' + row['id'][:8]}"
                for row in data["removed"]
            ],
        )


def _cmd_page(args):
    with _schematic(args) as (kicad, sch):
        page = sch.get_page_settings()
        title_block = sch.get_title_block()
        if args.size is not None:
            page.page_size = _PAGE_SIZES[args.size]
        if args.portrait is not None:
            page.orientation = (
                PageOrientation.PO_PORTRAIT if args.portrait else PageOrientation.PO_LANDSCAPE
            )
        if args.size is not None or args.portrait is not None:
            page = sch.set_page_settings(page)
        titles = {
            "title": args.title,
            "revision": args.rev,
            "date": args.date,
            "company": args.company,
        }
        retitled = any(value is not None for value in titles.values())
        if retitled:
            for name, value in titles.items():
                if value is not None:
                    setattr(title_block, name, value)
            sch.set_title_block(title_block)
            title_block = sch.get_title_block()
        if args.size is not None or args.portrait is not None or retitled:
            _save(kicad)

        portrait = page.orientation == PageOrientation.PO_PORTRAIT
        data = {
            "size": _PAGE_SIZE_NAMES.get(page.page_size, "custom"),
            "orientation": "portrait" if portrait else "landscape",
            **{name: getattr(title_block, name) or None for name in titles},
        }
        emit(
            args,
            data,
            lambda data: [
                f"page: {data['size']} {data['orientation']}",
                *(f"{name}: {data[name]}" for name in titles if data[name]),
            ],
        )


def _cmd_netlist(args):
    with _schematic(args) as (kicad, _sch):
        nets = netlist(kicad.get_schematic())
    data = {"nets": [{"name": name, "pins": nets[name]} for name in sorted(nets, key=natural)]}
    emit(
        args,
        data,
        lambda data: (
            [f"{net['name']}: {' '.join(net['pins'])}" for net in data["nets"]] or ["(no nets)"]
        ),
    )


def _cmd_erc(args):
    with _schematic(args) as (kicad, _sch):
        violations = run_erc(kicad.get_schematic())
    data = {"violations": [violation_data(violation, _at) for violation in violations]}
    emit(args, data, lambda data: violation_lines(data) or ["ERC reported no violations"])


def _cmd_render(args):
    schematic = resolve_schematic(args.file)
    if not schematic.exists():
        raise CliError(f"{schematic} does not exist; create it with 'cuprum new'")
    directory = render_directory(schematic, args.out)
    if args.area is None:
        files = render_schematic(schematic, directory, dpi=args.dpi, crop=not args.full_page)
        data = {"files": [str(path) for path in files], "area": None, "pixels_per_mil": None}
        emit(args, data, _render_lines)
        return
    corners = [_xy(token) for token in args.area]
    if None in corners:
        raise CliError("--area takes two corners as X,Y in mils")
    xs, ys = sorted(c.x / MIL for c in corners), sorted(c.y / MIL for c in corners)
    if xs[0] == xs[1] or ys[0] == ys[1]:
        raise CliError("--area takes two opposite corners of a rectangle")
    with connect(schematic) as kicad:
        sheet = _find_sheet(kicad.get_schematic().get_hierarchy(), args.sheet)
    area = area_data(xs, ys)
    name = schematic.stem if args.sheet is None else f"{schematic.stem}-{sheet.name}"
    output = directory / f"{name}-{area_row(area).replace(' -> ', '-')}.png"
    scale = render_schematic_area(
        schematic,
        output,
        (xs[0], ys[0], xs[1], ys[1]),
        page=sheet.page_number,
        page_file=schematic.parent / sheet.filename,
        dpi=args.dpi,
    )
    data = {"files": [str(output)], "area": area, "pixels_per_mil": number(scale, 6)}
    emit(args, data, _render_lines)


def _render_lines(data: dict) -> list[str]:
    lines = list(data["files"])
    if data["area"] is not None:
        scale = data["pixels_per_mil"] * 100
        lines.append(f"shows {area_row(data['area'])} at {scale:g} pixels per 100 mil")
    return lines


def _cmd_find(args):
    with _library(args) as kicad:
        if args.footprint:
            identifiers = kicad.get_library_footprints()
        else:
            identifiers = kicad.get_library_symbols()
        words = [word.lower() for argument in args.query for word in argument.split()]
        hits = []
        for identifier in identifiers:
            full_name = f"{identifier.library}:{identifier.name}"
            if all(word in full_name.lower() for word in words):
                exact = identifier.name.lower() in words
                hits.append((not exact, len(identifier.name), full_name, identifier))
        hits.sort(key=lambda hit: hit[:3])
        shown = hits[: args.limit]

        descriptions = {}
        if shown and not args.footprint:
            command = library_commands_pb2.GetItemsFromLibrary()
            command.type = LibraryType.LT_SYMBOL
            for *_rank, identifier in shown:
                command.item_ids.add().CopyFrom(identifier.proto)
            # The KiCad object has no public way to send a raw command, hence its private client.
            response = kicad._client.send(command, editor_commands_pb2.GetItemsResponse)
            for item in response.items:
                symbol = SchematicSymbol(proto=unpack_any(item))
                key = f"{symbol.id.library}:{symbol.id.name}"
                descriptions[key] = symbol.description_field.text.value

    matches = [
        {"lib_id": full_name, "description": descriptions.get(full_name) or None}
        for _exact, _length, full_name, _identifier in shown
    ]
    emit(args, {"matches": matches, "total": len(hits)}, _find_lines)


def _find_lines(data: dict) -> list[str]:
    lines = [f"{m['lib_id']}  {m['description'] or ''}".rstrip() for m in data["matches"]]
    if data["total"] > len(data["matches"]):
        more = data["total"] - len(data["matches"])
        lines.append(f"... {more} more; add a keyword or raise --limit")
    if not data["total"]:
        lines.append("nothing found")
    return lines


def _cmd_sym(args):
    with _library(args) as kicad:
        symbol = kicad.get_library_symbol(args.lib_id)
    emit(args, symbol_definition_data(args.lib_id, symbol), symbol_definition_lines)


def _cmd_kicad_cli(args):
    sys.stdout.flush()
    os.execv(kicad_cli(), ["kicad-cli", *args.arguments])


def _cmd_stop(args):
    path = resolve_schematic(args.file, required=False)
    emit(
        args,
        {"stopped": stop(path)},
        lambda data: [
            "stopped the background KiCad server"
            if data["stopped"]
            else "no background KiCad server running"
        ],
    )


# ---------------------------------------------------------------------------
# Arguments


def _build_parser() -> argparse.ArgumentParser:
    file_help = "schematic file; defaults to the only KiCad project in the current directory"
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS keeps a subcommand that was not given -f from overwriting a -f placed before it.
    common.add_argument("-f", "--file", default=argparse.SUPPRESS, help=file_help)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=JSON_HELP)
    on_sheet = argparse.ArgumentParser(add_help=False, parents=[common])
    on_sheet.add_argument(
        "--sheet", metavar="NAME", help="work on this sub-sheet (name or path) instead of the root"
    )

    parser = argparse.ArgumentParser(
        prog="cuprum",
        description="Edit a KiCad schematic through the IPC API. Coordinates are in mils on a "
        "50 mil grid. A position is either X,Y or REF.PIN (R1.2, U3.VDD). Changes are saved "
        "immediately. The board commands live under 'cuprum pcb'.",
    )
    parser.add_argument("-f", "--file", help=file_help)
    parser.add_argument("--json", action="store_true", help=JSON_HELP)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name, handler, help_text, *, parent=on_sheet, into=sub):
        subparser = into.add_parser(name, parents=[parent], help=help_text, description=help_text)
        subparser.set_defaults(handler=handler)
        return subparser

    p = command("new", _cmd_new, "create an empty schematic project", parent=common)
    p.add_argument("path", help="the .kicad_sch file to create")

    p = command(
        "show",
        _cmd_show,
        "list what is on the sheet; with references, show those symbols' pins and fields",
    )
    p.add_argument("references", nargs="*", metavar="REF")

    p = command("add", _cmd_add, "place a symbol from a library and print its pin positions")
    p.add_argument("lib_id", metavar="LIB:SYMBOL", help="e.g. Device:R or power:GND")
    p.add_argument(
        "at",
        metavar="POSITION",
        help="symbol origin; for a power symbol, the pin to attach to (C1.2)",
    )
    p.add_argument("--ref", help="reference; defaults to the next free one for the symbol's prefix")
    p.add_argument("--rot", type=int, choices=sorted(_ORIENTATIONS), default=0)
    p.add_argument("--mirror", choices=["x", "y"], help="x flips top-bottom, y flips left-right")
    p.add_argument("--unit", type=int, help="unit of a multi-unit device; requires --ref")
    p.add_argument("--value")
    p.add_argument("--footprint", help="e.g. Resistor_SMD:R_0603_1608Metric")
    p.add_argument(
        "--field", action="append", default=[], metavar="NAME=VALUE", help="e.g. MPN=RC0603"
    )

    p = command("set", _cmd_set, "change a placed symbol; moving it does not drag attached wires")
    p.add_argument("reference", metavar="REF")
    p.add_argument("--value")
    p.add_argument("--footprint")
    p.add_argument("--ref", help="new reference")
    p.add_argument("--at", metavar="X,Y", help="new symbol origin")
    p.add_argument("--rot", type=int, choices=sorted(_ORIENTATIONS))
    p.add_argument("--mirror", choices=["x", "y", "none"])
    p.add_argument("--unit", type=int, help="for a multi-unit device, change only this unit")
    p.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set a field such as MPN; an empty value removes it",
    )
    for option, text in (
        ("dnp", "do not populate the part"),
        ("bom", "list the part in the bill of materials"),
        ("board", "give the part a footprint on the board"),
        ("sim", "include the part in simulation"),
        ("pos", "list the part in position files"),
    ):
        p.add_argument(f"--{option}", action=argparse.BooleanOptionalAction, help=text)

    p = command(
        "field",
        _cmd_field,
        "move, hide or show a text of a placed symbol: its reference, its value or another field",
    )
    p.add_argument("reference", metavar="REF")
    p.add_argument("at", nargs="?", metavar="X,Y", help="where the text goes")
    p.add_argument(
        "--name", default="reference", help="reference (default), value, footprint or a field name"
    )
    p.add_argument("--unit", type=int, help="for a multi-unit device, this unit's text")
    visibility = p.add_mutually_exclusive_group()
    visibility.add_argument("--hide", dest="visible", action="store_false", default=None)
    visibility.add_argument("--show", dest="visible", action="store_true")

    p = command("wire", _cmd_wire, "draw wire segments through the given positions")
    p.add_argument("points", nargs="+", metavar="POSITION")

    p = command("label", _cmd_label, "place a net label (local unless --global or --hier)")
    p.add_argument("text", metavar="NET")
    p.add_argument("at", metavar="POSITION", help="on a wire or on a pin")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--global", dest="is_global", action="store_true")
    kind.add_argument(
        "--hier", action="store_true", help="pairs with a pin of this sheet's sheet symbol"
    )
    p.add_argument(
        "--spin", choices=sorted(_SPINS), default="right", help="direction the text extends in"
    )
    p.add_argument(
        "--shape", choices=sorted(_SHAPES), help="outline of a global or hierarchical label"
    )

    p = command(
        "junction", _marker_command(Junction, "junction", "junctions"), "place junction dots"
    )
    p.add_argument("points", nargs="+", metavar="POSITION")

    p = command(
        "nc",
        _marker_command(NoConnectMarker, "nc", "no_connects"),
        "place no-connect markers on pins",
    )
    p.add_argument("points", nargs="+", metavar="POSITION")

    p = command("text", _cmd_text, "place a text note")
    p.add_argument("text", metavar="TEXT")
    p.add_argument("at", metavar="POSITION", help="where the text starts: its bottom-left corner")

    p = command("edit", _cmd_edit, "move or reword a label or a text note")
    p.add_argument("target", metavar="ID")
    p.add_argument("--at", metavar="POSITION")
    p.add_argument("--text", help="new net name or new text")
    p.add_argument("--spin", choices=sorted(_SPINS), help="for a label, the way the text extends")

    p = command(
        "rm",
        _cmd_rm,
        "delete symbols by reference; wires, labels, junctions, markers, sheets and text by the "
        "ID from 'show'",
    )
    p.add_argument("targets", nargs="+", metavar="REF_OR_ID")

    command("sheets", _cmd_sheets, "list the sheets of the design", parent=common)

    sheet = sub.add_parser(
        "sheet", help="add a sub-sheet or a pin on one", description="hierarchical sheets"
    )
    sheet_sub = sheet.add_subparsers(dest="sheet_command", required=True, metavar="COMMAND")

    p = command(
        "add",
        _cmd_sheet_add,
        "draw a sheet symbol for a new sub-sheet; work inside it with --sheet NAME",
        into=sheet_sub,
    )
    p.add_argument("name", metavar="NAME")
    p.add_argument("at", metavar="POSITION", help="top-left corner of the sheet symbol")
    p.add_argument("--size", default="1500,1000", metavar="WIDTH,HEIGHT")
    p.add_argument(
        "--file-name", help="schematic file of the sub-sheet; defaults to NAME.kicad_sch"
    )

    p = command(
        "pin",
        _cmd_sheet_pin,
        "add a pin to a sheet symbol; it connects to the hierarchical label of the same name "
        "inside the sub-sheet",
        into=sheet_sub,
    )
    p.add_argument("sheet_name", metavar="SHEET", help="name of the sheet symbol")
    p.add_argument("name", metavar="PIN")
    p.add_argument("at", metavar="POSITION", help="moved onto the nearest edge of the sheet symbol")
    p.add_argument("--shape", choices=sorted(_SHAPES), default="input")

    p = command("page", _cmd_page, "show or set the page size and the title block")
    p.add_argument("--size", choices=sorted(_PAGE_SIZES, key=natural))
    orientation = p.add_mutually_exclusive_group()
    orientation.add_argument("--portrait", dest="portrait", action="store_true", default=None)
    orientation.add_argument("--landscape", dest="portrait", action="store_false")
    p.add_argument("--title")
    p.add_argument("--rev")
    p.add_argument("--date")
    p.add_argument("--company")

    command("netlist", _cmd_netlist, "export the pin-level connectivity of the whole design")
    command("erc", _cmd_erc, "run ERC on the whole design")

    p = command(
        "render",
        _cmd_render,
        "export every sheet as a PNG cropped to its contents and print the file paths",
    )
    p.add_argument(
        "--area",
        nargs=2,
        metavar="X,Y",
        help="plot only the rectangle between these two corners of one sheet (the root, or "
        "the one named with --sheet), enlarged: its longer side comes out up to 8 inches long "
        "at the given dpi",
    )
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument(
        "--out", help="output directory; defaults to a directory under the system temp dir"
    )
    p.add_argument(
        "--full-page", action="store_true", help="keep the whole page instead of cropping"
    )

    p = command(
        "find",
        _cmd_find,
        "search the libraries for symbols (or footprints) by name",
        parent=common,
    )
    p.add_argument(
        "query", nargs="+", metavar="KEYWORD", help="all must occur in LIB:NAME, case-insensitively"
    )
    p.add_argument("--footprint", action="store_true", help="search footprints instead of symbols")
    p.add_argument("--limit", type=int, default=20)

    p = command(
        "sym",
        _cmd_sym,
        "show a library symbol's description, default footprint and pins: number, name, type, "
        "connection point relative to the symbol origin (unrotated), and side of the body",
        parent=common,
    )
    p.add_argument("lib_id", metavar="LIB:SYMBOL")

    command("stop", _cmd_stop, "shut down this project's background KiCad server", parent=common)

    p = sub.add_parser(
        "kicad-cli",
        help="run KiCad's own kicad-cli, e.g. to export fabrication files",
        description="Run kicad-cli with the given arguments, wherever KiCad installed it.",
    )
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    p.set_defaults(handler=_cmd_kicad_cli)
    cli_lib.register(sub, common)
    cli_pcb.register(sub, common)
    return parser


if __name__ == "__main__":
    sys.exit(main())
