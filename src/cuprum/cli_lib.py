"""The ``cuprum lib`` commands, for parts the KiCad libraries lack, and ``cuprum fp``."""

from __future__ import annotations

import re
from contextlib import contextmanager

from kipy.errors import ApiError
from kipy.proto.board.board_types_pb2 import BoardLayer, PadType
from kipy.util.board_layer import is_copper_layer

from cuprum.cli_common import (
    CliError,
    courtyard_data,
    courtyard_row,
    emit,
    mm,
    natural,
    num,
    resolve_schematic,
    section_lines,
    symbol_definition_data,
    symbol_definition_lines,
    xy,
)
from cuprum.library import (
    PAD_SHAPES,
    PIN_SIDES,
    PIN_TYPES,
    Pad,
    SymbolPin,
    library_name,
    write_footprint,
    write_symbol,
)
from cuprum.session import EditorNotOpen, connect, stop

_PASTE = re.compile(r"paste=([1-9]\d*)x([1-9]\d*)(?:/(\d+(?:\.\d+)?))?")
_PASTE_PERCENT = 65


def _project(args):
    schematic = resolve_schematic(args.file)
    project = schematic.with_suffix(".kicad_pro")
    if not project.exists():
        raise CliError(f"{project} does not exist; create the project with 'cuprum new'")
    return schematic, project


def _restart(schematic) -> None:
    """Make KiCad see the changed library. A running server reads the library tables once, when
    it opens the project, so the only reliable way is to start a new one."""
    stop(schematic)


@contextmanager
def _loaded(lib_id: str):
    """Reading a part back right after writing it fails when the KiCad answering is a GUI that
    has the project open: it cannot be restarted from here and has not loaded the library."""
    try:
        yield
    except (ApiError, EditorNotOpen):
        raise CliError(
            f"wrote {lib_id}, but the running KiCad has not loaded it; reopen the project there"
        ) from None


# ---------------------------------------------------------------------------
# Parsing


def _pin(spec: str) -> SymbolPin:
    fields = spec.split(":")
    if len(fields) != 4:
        raise CliError(f"bad pin {spec!r}: expected NUMBER:NAME:TYPE:SIDE")
    number, name, pin_type, side = fields
    if pin_type not in PIN_TYPES:
        raise CliError(f"bad pin type {pin_type!r}: one of {', '.join(PIN_TYPES)}")
    if side not in PIN_SIDES:
        raise CliError(f"bad pin side {side!r}: one of {', '.join(PIN_SIDES)}")
    return SymbolPin(number, name, pin_type, side)


def _pair(text: str, separator: str, what: str) -> tuple[float, float]:
    try:
        first, second = text.split(separator)
        return float(first), float(second)
    except ValueError:
        raise CliError(f"bad {what} {text!r}") from None


def _pad_options(options: list[str], spec: str) -> dict:
    """The optional parts of a pad: its shape, drill and paste."""
    chosen = {"shape": None, "drill": None, "paste": None}
    for option in options:
        if option in PAD_SHAPES:
            chosen["shape"] = option
        elif option.startswith("drill="):
            try:
                chosen["drill"] = float(option.removeprefix("drill="))
            except ValueError:
                raise CliError(f"bad drill in {spec!r}") from None
        elif option == "paste=none":
            chosen["paste"] = (1, 1, 0.0)
        elif match := _PASTE.fullmatch(option):
            share = float(match[3] or _PASTE_PERCENT) / 100
            if not 0 < share <= 1:
                raise CliError(f"bad paste share in {spec!r}: a percentage up to 100")
            chosen["paste"] = (int(match[1]), int(match[2]), share)
        else:
            raise CliError(
                f"bad option {option!r} in {spec!r}: a shape, drill=MM, "
                "paste=COLUMNSxROWS[/PERCENT] or paste=none"
            )
    if chosen["drill"] is not None and chosen["paste"] is not None:
        raise CliError(f"bad pad {spec!r}: a pad with a hole gets no paste anyway")
    return chosen


def _new_pad(number: str, x: float, y: float, size: tuple[float, float], options: dict) -> Pad:
    shape, drill = options["shape"], options["drill"]
    if shape is None:
        shape = "rect" if drill is None else "circle" if size[0] == size[1] else "oval"
    return Pad(number, x, y, size[0], size[1], shape, drill, options["paste"])


def _pads(pad_specs: list[str], row_specs: list[str]) -> list[Pad]:
    pads = []
    for spec in row_specs:
        fields = spec.split(":")
        if len(fields) < 4:
            raise CliError(f"bad row {spec!r}: expected FIRST-LAST:X,Y:DX,DY:WxH")
        try:
            first, last = (int(number) for number in fields[0].split("-"))
        except ValueError:
            raise CliError(f"bad pad numbers in row {spec!r}: expected FIRST-LAST") from None
        x, y = _pair(fields[1], ",", "position")
        dx, dy = _pair(fields[2], ",", "pitch")
        size = _pair(fields[3], "x", "size")
        options = _pad_options(fields[4:], spec)
        step = 1 if last >= first else -1
        for index, number in enumerate(range(first, last + step, step)):
            pads.append(_new_pad(str(number), x + index * dx, y + index * dy, size, options))
    for spec in pad_specs:
        fields = spec.split(":")
        if len(fields) < 3:
            raise CliError(f"bad pad {spec!r}: expected NUMBER:X,Y:WxH")
        x, y = _pair(fields[1], ",", "position")
        options = _pad_options(fields[3:], spec)
        pads.append(_new_pad(fields[0], x, y, _pair(fields[2], "x", "size"), options))
    if not pads:
        raise CliError("a footprint needs pads: give --pad or --row")
    return sorted(pads, key=lambda pad: natural(pad.number))


# ---------------------------------------------------------------------------
# Output


def library_footprint_data(kicad, lib_id: str) -> dict:
    """A library footprint: its courtyard and each pad's number, position relative to the
    footprint origin, size, kind and solder paste, in millimetres."""
    footprint = kicad.get_library_footprint(lib_id)
    # A pad that is on no copper layer is an opening in the paste stencil or the solder mask.
    windows = [p for p in footprint.pads if not any(map(is_copper_layer, p.padstack.layers))]
    pads = [p for p in footprint.pads if any(map(is_copper_layer, p.padstack.layers))]
    pads.sort(key=lambda pad: natural(pad.number))
    through_hole = any(pad.pad_type == PadType.PT_PTH for pad in pads)
    layers = (BoardLayer.BL_F_CrtYd, BoardLayer.BL_B_CrtYd)
    boxes = [shape.bounding_box() for shape in footprint.shapes if shape.layer in layers]
    courtyard = None
    if boxes:
        courtyard = courtyard_data(
            min(box.pos.x for box in boxes),
            min(box.pos.y for box in boxes),
            max(box.pos.x + box.size.x for box in boxes),
            max(box.pos.y + box.size.y for box in boxes),
        )
    rows = []
    for pad in pads:
        copper = pad.padstack.copper_layers
        rows.append(
            {
                "number": pad.number or None,
                "position": {"x": mm(pad.position.x), "y": mm(pad.position.y)},
                "width": mm(copper[0].size.x) if copper else None,
                "height": mm(copper[0].size.y) if copper else None,
                "type": {PadType.PT_PTH: "thru", PadType.PT_SMD: "smd"}.get(pad.pad_type, "other"),
                "paste": _paste_data(pad, windows),
            }
        )
    return {
        "lib_id": lib_id,
        "mount": "through_hole" if through_hole else "smd",
        "courtyard": courtyard,
        "pads": rows,
    }


def _paste_data(pad, windows) -> dict | None:
    """How the solder paste of an SMD pad differs from one opening as large as the pad: the
    number of openings and the share of the pad they cover. None when it does not differ."""
    layers, copper = pad.padstack.layers, pad.padstack.copper_layers
    paste_layers = (BoardLayer.BL_F_Paste, BoardLayer.BL_B_Paste)
    if pad.pad_type != PadType.PT_SMD or not copper or any(x in layers for x in paste_layers):
        return None
    size = copper[0].size
    inside = [
        window.padstack.copper_layers[0].size
        for window in windows
        if any(x in window.padstack.layers for x in paste_layers)
        and abs(window.position.x - pad.position.x) <= size.x / 2
        and abs(window.position.y - pad.position.y) <= size.y / 2
    ]
    share = sum(window.x * window.y for window in inside) / (size.x * size.y)
    return {"windows": len(inside), "coverage": round(share, 4)}


def library_footprint_lines(data: dict) -> list[str]:
    lines = [f"{data['lib_id']}  {data['mount']}  {len(data['pads'])} pads"]
    if data["courtyard"] is not None:
        lines.append(courtyard_row(data["courtyard"]))
    for pad in data["pads"]:
        size = "" if pad["width"] is None else f"{num(pad['width'])}x{num(pad['height'])}"
        paste = pad["paste"]
        if paste is None:
            paste = ""
        elif not paste["windows"]:
            paste = "  no paste"
        else:
            count = "1 window" if paste["windows"] == 1 else f"{paste['windows']} windows"
            paste = f"  paste in {count} covering {paste['coverage']:.0%}"
        number = pad["number"] or "~"
        lines.append(f"  {number:<4} @{xy(pad['position'])}  {size}  {pad['type']}{paste}".rstrip())
    return lines


# ---------------------------------------------------------------------------
# Commands


def _cmd_symbol(args):
    schematic, project = _project(args)
    write_symbol(
        project,
        args.name,
        [_pin(spec) for spec in args.pin],
        reference=args.ref,
        footprint=args.footprint,
        description=args.description,
        datasheet=args.datasheet,
    )
    _restart(schematic)
    lib_id = f"{library_name(project)}:{args.name}"
    with _loaded(lib_id), connect(schematic) as kicad:
        symbol = kicad.get_library_symbol(lib_id)
    emit(args, symbol_definition_data(lib_id, symbol), symbol_definition_lines)


def _cmd_footprint(args):
    schematic, project = _project(args)
    body = _pair(args.body, "x", "body size") if args.body else None
    write_footprint(
        project,
        args.name,
        _pads(args.pad, args.row),
        body=body,
        description=args.description,
        silk=args.silk,
    )
    _restart(schematic)
    lib_id = f"{library_name(project)}:{args.name}"
    with _loaded(lib_id), connect(schematic) as kicad:
        data = library_footprint_data(kicad, lib_id)
    emit(args, data, library_footprint_lines)


def _cmd_list(args):
    schematic, project = _project(args)
    nickname = library_name(project)
    data = {}
    with connect(schematic) as kicad:
        for title, getter in (
            ("symbols", kicad.get_library_symbols),
            ("footprints", kicad.get_library_footprints),
        ):
            try:
                names = sorted((item.name for item in getter([nickname])), key=natural)
            except ApiError:
                names = []  # the project has no library of this kind yet
            data[title] = [f"{nickname}:{name}" for name in names]
    emit(args, data, section_lines)


def _cmd_fp(args):
    schematic = resolve_schematic(args.file, required=False)
    with connect(schematic if schematic is not None and schematic.exists() else None) as kicad:
        data = library_footprint_data(kicad, args.lib_id)
    emit(args, data, library_footprint_lines)


# ---------------------------------------------------------------------------
# Arguments


def register(subparsers, common) -> None:
    """Add ``fp`` and the ``lib`` command group to the top-level parser."""
    p = subparsers.add_parser(
        "fp",
        parents=[common],
        help="show a library footprint's courtyard and pads",
        description="Show a library footprint: its courtyard and each pad's number, position "
        "relative to the footprint origin, size and kind. Millimetres.",
    )
    p.add_argument("lib_id", metavar="LIB:FOOTPRINT")
    p.set_defaults(handler=_cmd_fp)

    summary = "create symbols and footprints for parts the KiCad libraries lack"
    lib = subparsers.add_parser(
        "lib",
        help=summary,
        description=summary + ". They go into a library that belongs to the project and is "
        "named after it, so the new part is PROJECT:NAME.",
    )
    sub = lib.add_subparsers(dest="lib_command", required=True, metavar="COMMAND")

    def command(name, handler, help_text):
        subparser = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        subparser.set_defaults(handler=handler)
        return subparser

    p = command(
        "symbol",
        _cmd_symbol,
        "create or replace a symbol: a box with the pins of each side in the order given, top to "
        "bottom and left to right",
    )
    p.add_argument("name", metavar="NAME")
    p.add_argument(
        "--pin",
        action="append",
        default=[],
        required=True,
        metavar="NUMBER:NAME:TYPE:SIDE",
        help=f"TYPE is one of {', '.join(PIN_TYPES)}; SIDE is one of {', '.join(PIN_SIDES)}",
    )
    p.add_argument("--ref", default="U", help="reference prefix (default U)")
    p.add_argument("--footprint", default="", metavar="LIB:NAME", help="default footprint")
    p.add_argument("--description", default="")
    p.add_argument("--datasheet", default="")

    p = command(
        "footprint",
        _cmd_footprint,
        "create or replace a footprint from pads given in millimetres relative to its origin, "
        "y pointing down; the courtyard is added, the silkscreen is left empty",
    )
    p.add_argument("name", metavar="NAME")
    p.add_argument(
        "--pad",
        action="append",
        default=[],
        metavar="NUMBER:X,Y:WxH[:SHAPE][:drill=MM][:paste=COLUMNSxROWS[/PERCENT]]",
        help=f"SHAPE is one of {', '.join(PAD_SHAPES)} (default rect); drill makes it "
        "through-hole. paste=2x2 splits the paste opening of a large pad, such as the exposed "
        "pad under a chip, into a grid of windows that cover 65 percent of it, or the "
        "percentage given; a solid opening that size tends to float the part. paste=1x1/80 is "
        "one smaller opening, paste=none leaves the pad without paste",
    )
    p.add_argument(
        "--row",
        action="append",
        default=[],
        metavar="FIRST-LAST:X,Y:DX,DY:WxH[:SHAPE][:drill=MM][:paste=...]",
        help="a row of pads numbered FIRST to LAST, starting at X,Y and stepping by DX,DY",
    )
    p.add_argument("--body", metavar="WxH", help="package body, drawn on the fabrication layer")
    p.add_argument("--description", default="")
    p.add_argument("--silk", action="store_true", help="add a pin-1 dot on the silkscreen")

    command("list", _cmd_list, "list the symbols and footprints of the project's library")
