"""The ``cuprum lib`` commands, for parts the KiCad libraries lack, and ``cuprum fp``."""

from __future__ import annotations

import argparse
import re
from contextlib import contextmanager
from pathlib import Path

from kipy.errors import ApiError
from kipy.proto.board.board_types_pb2 import BoardLayer, PadType
from kipy.proto.common.types import LibraryTableScope, LibraryType
from kipy.util.board_layer import is_copper_layer

from cuprum.board import pad_layer
from cuprum.cli_common import (
    CliError,
    courtyard_data,
    courtyard_row,
    emit,
    mm,
    natural,
    num,
    pad_kind,
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
    PinGap,
    SymbolPin,
    import_footprint,
    import_symbol,
    in_library_table,
    libraries_at,
    library_name,
    part_names,
    write_footprint,
    write_symbol,
)
from cuprum.session import EditorNotOpen, connect, stop

_PASTE = re.compile(r"paste=([1-9]\d*)x([1-9]\d*)(?:/(\d+(?:\.\d+)?))?")
_PASTE_PERCENT = 65
_TABLES = {LibraryType.LT_SYMBOL: "sym-lib-table", LibraryType.LT_FOOTPRINT: "fp-lib-table"}


def _project(args):
    schematic = resolve_schematic(args.file)
    project = schematic.with_suffix(".kicad_pro")
    if not project.exists():
        raise CliError(f"{project} does not exist; create the project with 'cuprum new'")
    return schematic, project


def _known(project, kind) -> bool:
    """Whether a KiCad that has opened the project knows the project's library of this kind."""
    return in_library_table(project, _TABLES[kind])


def _refreshed(schematic, project, kind, known: bool):
    """A connection to a KiCad that has read the project's library again after it changed.

    A KiCad that knew the library reloads it. A library that is new to the project is in the
    library table, which KiCad only reads when it opens the project: the background server is
    started again for that, and also when the reload fails.
    """
    if known:
        kicad = connect(schematic)
        try:
            kicad.reload_library(kind, LibraryTableScope.LTS_PROJECT, [library_name(project)])
            kicad.wait_for_libraries(kind, interval_ms=50, scope=LibraryTableScope.LTS_PROJECT)
            return kicad
        except ApiError:
            kicad.close()
    stop(schematic)
    return connect(schematic)


@contextmanager
def _loaded(lib_id: str):
    """Reading a part back right after writing it fails when the KiCad answering is a GUI that
    has the project open and has not loaded the library: it cannot be restarted from here."""
    try:
        yield
    except (ApiError, EditorNotOpen):
        raise CliError(
            f"wrote {lib_id}, but the running KiCad has not loaded it; reopen the project there"
        ) from None


# ---------------------------------------------------------------------------
# Parsing


def _pin_type(name: str) -> str:
    if name not in PIN_TYPES:
        raise CliError(f"bad pin type {name!r}: one of {', '.join(PIN_TYPES)}")
    return name


def _pin(spec: str) -> SymbolPin:
    fields = spec.split(":")
    if len(fields) < 4:
        raise CliError(f"bad pin {spec!r}: expected NUMBER[,NUMBER...]:NAME:TYPE:SIDE")
    numbers, name, pin_type, side = fields[:4]
    numbers = numbers.split(",")
    if not all(numbers):
        raise CliError(f"bad pin {spec!r}: a pin number is empty")
    if side not in PIN_SIDES:
        raise CliError(f"bad pin side {side!r}: one of {', '.join(PIN_SIDES)}")
    alternates = []
    for option in fields[4:]:
        function, slash, kind = option.removeprefix("alt=").rpartition("/")
        if not (option.startswith("alt=") and function and slash):
            raise CliError(f"bad option {option!r} in {spec!r}: expected alt=NAME/TYPE")
        alternates.append((function, _pin_type(kind)))
    return SymbolPin(
        numbers[0], name, _pin_type(pin_type), side, tuple(numbers[1:]), tuple(alternates)
    )


def _gap(side: str) -> PinGap:
    """The value of --gap, which argparse keeps in one list with the pins, in the order given."""
    if side not in PIN_SIDES:
        raise argparse.ArgumentTypeError(f"bad side {side!r}: one of {', '.join(PIN_SIDES)}")
    return PinGap(side)


def _symbol_pins(items: list) -> list[SymbolPin | PinGap]:
    items = [item if isinstance(item, PinGap) else _pin(item) for item in items]
    numbers = [n for item in items if isinstance(item, SymbolPin) for n in _numbers(item)]
    twice = sorted({n for n in numbers if numbers.count(n) > 1}, key=natural)
    if twice:
        raise CliError(f"pin number given more than once: {', '.join(twice)}")
    return items


def _numbers(pin: SymbolPin) -> tuple[str, ...]:
    return (pin.number, *pin.stacked)


def _pair(text: str, separator: str, what: str) -> tuple[float, float]:
    try:
        first, second = text.split(separator)
        return float(first), float(second)
    except ValueError:
        raise CliError(f"bad {what} {text!r}") from None


def _pad_options(options: list[str], spec: str) -> dict:
    """The optional parts of a pad: its shape, drill, paste and side."""
    chosen = {"shape": None, "drill": None, "paste": None, "back": False}
    for option in options:
        if option in PAD_SHAPES:
            chosen["shape"] = option
        elif option == "back":
            chosen["back"] = True
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
                "paste=COLUMNSxROWS[/PERCENT], paste=none or back"
            )
    if chosen["drill"] is not None and chosen["paste"] is not None:
        raise CliError(f"bad pad {spec!r}: a pad with a hole gets no paste anyway")
    if chosen["drill"] is not None and chosen["back"]:
        raise CliError(f"bad pad {spec!r}: a pad with a hole is on both sides anyway")
    return chosen


def _new_pad(number: str, x: float, y: float, size: tuple[float, float], options: dict) -> Pad:
    shape, drill = options["shape"], options["drill"]
    if shape is None:
        shape = "rect" if drill is None else "circle" if size[0] == size[1] else "oval"
    return Pad(number, x, y, size[0], size[1], shape, drill, options["paste"], options["back"])


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
    footprint origin, size, kind, copper layer and solder paste, in millimetres."""
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
                "layer": pad_layer(pad),
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
        lines.append(
            f"  {number:<4} @{xy(pad['position'])}  {size}  {pad_kind(pad)}{paste}".rstrip()
        )
    return lines


# ---------------------------------------------------------------------------
# Commands


def _emit_symbol(args, schematic, project, name: str, known: bool) -> None:
    """Print a symbol of the project's library as KiCad reads it after the change."""
    lib_id = f"{library_name(project)}:{name}"
    with _loaded(lib_id), _refreshed(schematic, project, LibraryType.LT_SYMBOL, known) as kicad:
        symbol = kicad.get_library_symbol(lib_id)
    emit(args, symbol_definition_data(lib_id, symbol), symbol_definition_lines)


def _emit_footprint(args, schematic, project, name: str, known: bool) -> None:
    """Print a footprint of the project's library as KiCad reads it after the change."""
    lib_id = f"{library_name(project)}:{name}"
    with _loaded(lib_id), _refreshed(schematic, project, LibraryType.LT_FOOTPRINT, known) as kicad:
        data = library_footprint_data(kicad, lib_id)
    emit(args, data, library_footprint_lines)


def _cmd_symbol(args):
    schematic, project = _project(args)
    known = _known(project, LibraryType.LT_SYMBOL)
    write_symbol(
        project,
        args.name,
        _symbol_pins(args.pin),
        reference=args.ref,
        footprint=args.footprint,
        description=args.description,
        datasheet=args.datasheet,
        keywords=args.keywords,
        footprint_filters=tuple(args.fp_filter),
    )
    _emit_symbol(args, schematic, project, args.name, known)


def _cmd_footprint(args):
    schematic, project = _project(args)
    body = _pair(args.body, "x", "body size") if args.body else None
    known = _known(project, LibraryType.LT_FOOTPRINT)
    write_footprint(
        project,
        args.name,
        _pads(args.pad, args.row),
        body=body,
        description=args.description,
        silk=args.silk,
    )
    _emit_footprint(args, schematic, project, args.name, known)


def _cmd_import(args):
    schematic, project = _project(args)
    source = Path(args.path).expanduser().resolve()
    footprint = None  # the footprint file to copy; a symbol otherwise
    if source.suffix == ".kicad_mod" and source.is_file():
        if args.name:
            raise CliError(f"{source.name} is one footprint: leave out {args.name}")
        footprint = source
    elif not libraries_at(source) or source.suffix not in (".kicad_sym", ".pretty"):
        raise CliError(
            f"{args.path} is not a .kicad_sym file, a .pretty folder or a .kicad_mod file"
        )
    elif not args.name:
        raise CliError(f"name the part to take from {source.name}; 'lib list' shows them")
    elif source.suffix == ".pretty":
        footprint = source / f"{args.name}.kicad_mod"
        if not footprint.is_file():
            raise CliError(f"{source.name} has no footprint {args.name}")

    if footprint is not None:
        if args.footprint:
            raise CliError("--footprint sets the default footprint of a symbol")
        known = _known(project, LibraryType.LT_FOOTPRINT)
        import_footprint(project, footprint)
        _emit_footprint(args, schematic, project, footprint.stem, known)
        return
    known = _known(project, LibraryType.LT_SYMBOL)
    try:
        import_symbol(project, source, args.name, footprint=args.footprint)
    except ValueError as error:
        raise CliError(str(error)) from None
    _emit_symbol(args, schematic, project, args.name, known)


def _outside_lines(data: dict) -> list[str]:
    lines = []
    for library in data["libraries"]:
        lines.append(f"{library['path']}  {library['kind']}:")
        lines += [f"  {name}" for name in library["names"]]
    return lines


def _cmd_list(args):
    if args.path:
        path = Path(args.path).expanduser().resolve()
        libraries = libraries_at(path)
        if not libraries:
            raise CliError(f"{args.path} is no .kicad_sym file or .pretty folder and holds none")
        data = {
            "libraries": [
                {
                    "path": str(library),
                    "kind": "footprints" if library.suffix == ".pretty" else "symbols",
                    "names": sorted(part_names(library), key=natural),
                }
                for library in libraries
            ]
        }
        emit(args, data, _outside_lines)
        return
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

    summary = "create or bring in symbols and footprints for parts the KiCad libraries lack"
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
        metavar="NUMBER[,NUMBER...]:NAME:TYPE:SIDE[:alt=NAME/TYPE]...",
        help=f"TYPE is one of {', '.join(PIN_TYPES)}; SIDE is one of {', '.join(PIN_SIDES)}. "
        "Several numbers are pads that are the same node, a stack: one pin shows and a wire to "
        "it connects them all. alt=NAME/TYPE adds another function the pin can take",
    )
    p.add_argument(
        "--gap",
        dest="pin",
        action="append",
        type=_gap,
        metavar="SIDE",
        help="leave the next position on that side free, between two groups of pins; --pin and "
        "--gap take effect in the order given",
    )
    p.add_argument("--ref", default="U", help="reference prefix (default U)")
    p.add_argument("--footprint", default="", metavar="LIB:NAME", help="default footprint")
    p.add_argument("--description", default="")
    p.add_argument("--datasheet", default="")
    p.add_argument(
        "--keywords", default="", help="words by which KiCad's symbol chooser finds the part"
    )
    p.add_argument(
        "--fp-filter",
        action="append",
        default=[],
        metavar="PATTERN",
        help="a pattern for the names of the footprints that fit, such as SOT?23*",
    )

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
        metavar="NUMBER:X,Y:WxH[:SHAPE][:drill=MM][:paste=COLUMNSxROWS[/PERCENT]][:back]",
        help=f"SHAPE is one of {', '.join(PAD_SHAPES)} (default rect); drill makes it "
        "through-hole. paste=2x2 splits the paste opening of a large pad, such as the exposed "
        "pad under a chip, into a grid of windows that cover 65 percent of it, or the "
        "percentage given; a solid opening that size tends to float the part. paste=1x1/80 is "
        "one smaller opening, paste=none leaves the pad without paste. back puts the pad on "
        "the back of the board, for a part that clamps the board edge; its position stays the "
        "one seen from the front",
    )
    p.add_argument(
        "--row",
        action="append",
        default=[],
        metavar="FIRST-LAST:X,Y:DX,DY:WxH[:SHAPE][:drill=MM][:paste=...][:back]",
        help="a row of pads numbered FIRST to LAST, starting at X,Y and stepping by DX,DY",
    )
    p.add_argument("--body", metavar="WxH", help="package body, drawn on the fabrication layer")
    p.add_argument("--description", default="")
    p.add_argument("--silk", action="store_true", help="add a pin-1 dot on the silkscreen")

    p = command(
        "import",
        _cmd_import,
        "copy a symbol or a footprint into the project's library from a library file that "
        "KiCad's library tables do not list, replacing one of the same name",
    )
    p.add_argument(
        "path",
        metavar="PATH",
        help="a .kicad_sym file or a .pretty folder, or the .kicad_mod file of the footprint",
    )
    p.add_argument("name", nargs="?", metavar="NAME", help="the part to take from the library")
    p.add_argument(
        "--footprint",
        metavar="LIB:NAME",
        help="default footprint of the imported symbol, which otherwise still names the "
        "library the symbol came from",
    )

    p = command(
        "list",
        _cmd_list,
        "list the symbols and footprints of the project's library, or of library files "
        "outside KiCad's library tables",
    )
    p.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="a .kicad_sym file, a .pretty folder, or a folder that holds such libraries",
    )
