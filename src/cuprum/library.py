"""Project-local library items: symbols and footprints for parts the KiCad libraries lack.

These are written as KiCad library files rather than through the IPC API. In the current KiCad
the API can open an existing library symbol or footprint and edit its children, but it cannot
create one, name it or set its fields, and the library-table commands have no handler. So the
files are generated here, and the API is used afterwards to load them and read them back.

A project gets one symbol library, ``NAME.kicad_sym``, and one footprint library, ``NAME.pretty``,
both next to the project file and both registered in the project's library tables under the
project's name. Parts from library files that KiCad's tables do not list are copied into them.
"""

from __future__ import annotations

import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cuprum.export import upgrade_symbol_library

MIL = 0.0254  # mm

# What `cuprum sym` prints for a pin type -> the keyword of the symbol file format.
PIN_TYPES = {
    "input": "input",
    "output": "output",
    "bidirectional": "bidirectional",
    "tristate": "tri_state",
    "passive": "passive",
    "free": "free",
    "unspecified": "unspecified",
    "power_input": "power_in",
    "power_output": "power_out",
    "open_collector": "open_collector",
    "open_emitter": "open_emitter",
    "no_connect": "no_connect",
}
PIN_SIDES = ("left", "right", "top", "bottom")
PAD_SHAPES = ("rect", "roundrect", "oval", "circle")

_SYMBOL_VERSION = 20231120  # the file format that _symbol() writes
_STRING = r'"((?:[^"\\]|\\.)*)"'
_GRID = 100  # mil; pins sit on this grid
_CHAR_WIDTH = 50  # mil, what a capital letter or a digit takes in the default 50 mil text
# In a stack of pins of these types, KiCad's library convention keeps the type on the pin that
# shows and makes the others passive: several of them on one net are a conflict to ERC.
_SINGLE_TYPES = ("output", "power_output", "power_input")


@dataclass(frozen=True)
class SymbolPin:
    number: str
    name: str
    type: str
    side: str
    stacked: tuple[str, ...] = ()
    """The numbers of further pads that are the same node. Each becomes a hidden pin on the
    connection point of this one, so a wire that ends there connects them all."""
    alternates: tuple[tuple[str, str], ...] = ()
    """The other functions the pin can take, each as a name and a type."""


@dataclass(frozen=True)
class PinGap:
    """A grid position on a side that stays free, to set groups of pins apart."""

    side: str


@dataclass(frozen=True)
class Pad:
    number: str
    x: float
    y: float
    width: float
    height: float
    shape: str = "rect"
    drill: float | None = None
    """Hole diameter; a pad with a hole goes through all copper layers."""
    paste: tuple[int, int, float] | None = None
    """Columns, rows and the share of the pad they cover: the solder paste goes on as that many
    separate windows instead of one opening as large as the pad. A share of 0 means no paste."""
    back: bool = False
    """On the back of the board, as the pads of one row of a part that clamps the board edge
    are. The position stays the one seen from the front."""


def library_name(project_file: Path) -> str:
    return project_file.stem


def symbol_library(project_file: Path) -> Path:
    return project_file.with_suffix(".kicad_sym")


def footprint_library(project_file: Path) -> Path:
    return project_file.with_suffix(".pretty")


# ---------------------------------------------------------------------------
# Symbols


def write_symbol(
    project_file: Path,
    name: str,
    pins: list[SymbolPin | PinGap],
    *,
    reference: str = "U",
    footprint: str = "",
    description: str = "",
    datasheet: str = "",
    keywords: str = "",
    footprint_filters: tuple[str, ...] = (),
) -> Path:
    """Add the symbol to the project's symbol library, replacing one of the same name, and
    register the library. Returns the library file.

    The symbol is a box. The pins of a side follow each other in the order given, top to bottom
    or left to right, one grid position each; a :class:`PinGap` leaves one free.
    """
    library = symbol_library(project_file)
    text = library.read_text() if library.exists() else ""
    symbols = _symbols(text)
    metadata = {
        "Footprint": footprint,
        "Datasheet": datasheet,
        "Description": description,
        "ki_keywords": keywords,
        "ki_fp_filters": " ".join(footprint_filters),
    }
    symbols[name] = _symbol(name, pins, reference, metadata)
    # The file keeps a later version that it has: symbols KiCad wrote in that format stay in it.
    _save_symbols(library, max(_version(text), _SYMBOL_VERSION), symbols)
    _register(project_file, "sym-lib-table", "sym_lib_table", library)
    return library


def import_symbol(
    project_file: Path, source: Path, name: str, *, footprint: str | None = None
) -> Path:
    """Copy the symbol ``name`` of the library file ``source`` into the project's symbol
    library, replacing one of the same name, and register the library. A symbol derived from
    another brings that one along. ``footprint`` replaces the default footprint, which
    otherwise still names the library the symbol came from. Returns the library file.

    How a symbol is read depends on the version its file states, and a file states one for all
    of its symbols. So KiCad first rewrites both libraries in its current format.
    """
    library = symbol_library(project_file)
    with tempfile.TemporaryDirectory() as scratch:
        current = Path(scratch) / source.name
        upgrade_symbol_library(source, current)
        text = current.read_text()
    available = _symbols(text)
    chain = []  # the symbol, and before it the ones it is derived from
    parent = name
    while parent is not None and parent not in chain:
        if parent not in available:
            raise ValueError(f"{source.name} has no symbol {parent}")
        chain.insert(0, parent)
        extends = re.search(rf"\(extends {_STRING}\)", available[parent])
        parent = _unquote(extends[1]) if extends else None

    if library.exists() and _version(library.read_text()) != _version(text):
        upgrade_symbol_library(library)
    symbols = _symbols(library.read_text()) if library.exists() else {}
    for part in chain:
        symbols[part] = available[part]
    if footprint is not None:
        symbols[name] = re.sub(
            rf'(\(property\s+"Footprint"\s+){_STRING}',
            lambda match: match[1] + _quote(footprint),
            symbols[name],
            count=1,
        )
    _save_symbols(library, _version(text), symbols)
    _register(project_file, "sym-lib-table", "sym_lib_table", library)
    return library


def _symbols(text: str) -> dict[str, str]:
    """The symbols of a library file by name, each as the text of its list, in file order."""
    symbols = {}
    for block in _children(text):
        name = re.match(rf"\(symbol\s+{_STRING}", block)
        if name:
            symbols[_unquote(name[1])] = block
    return symbols


def _version(text: str) -> int:
    version = re.search(r"\(version (\d+)\)", text)
    return int(version[1]) if version else 0


def _save_symbols(library: Path, version: int, symbols: dict[str, str]) -> None:
    library.write_text(
        f'(kicad_symbol_lib\n\t(version {version})\n\t(generator "cuprum")\n'
        + "".join(f"\t{block}\n" for block in symbols.values())
        + ")\n"
    )


def _pin_length(pins: list[SymbolPin]) -> int:
    """The length of all pins of a symbol, in mils, after KiCad's library convention: 100, and
    more where the longest pin number needs the room."""
    numbers = [number for pin in pins for number in (pin.number, *pin.stacked)]
    digits = max(map(len, numbers), default=0)
    return 100 if digits <= 2 else 150 if digits == 3 else 200


def _symbol(name: str, items: list[SymbolPin | PinGap], reference: str, metadata: dict) -> str:
    # What each side holds, a gap taking a position like a pin does.
    left, right, top, bottom = ([x for x in items if x.side == side] for side in PIN_SIDES)
    pins = [item for item in items if isinstance(item, SymbolPin)]
    length = _pin_length(pins)

    def longest(side) -> int:
        names = [len(item.name) for item in side if isinstance(item, SymbolPin)]
        return max(names, default=0) * _CHAR_WIDTH

    def has_pins(*sides) -> bool:
        return any(isinstance(item, SymbolPin) for side in sides for item in side)

    def up(mils: float) -> int:
        return math.ceil(mils / _GRID) * _GRID

    def edge(reach: float, with_pins: bool) -> tuple[int, int]:
        """How far from the origin the pins of an edge end, which is on the grid, and where
        that puts the edge itself: at least ``reach`` out."""
        stub = length if with_pins else 0
        end = up(reach + stub)
        return end, end - stub

    # Side pins run down from the top on the grid, with the symbol origin near the middle.
    rows = max(len(left), len(right), 1)
    first_row = -((rows - 1) // 2) * _GRID
    last_row = first_row + (rows - 1) * _GRID
    # Pins on the top and bottom carry their names inside the body, vertically.
    top_end, body_top = (-x for x in edge(_GRID - first_row + up(longest(top)), has_pins(top)))
    bottom_end, body_bottom = edge(last_row + _GRID + up(longest(bottom)), has_pins(bottom))
    columns = max(len(top), len(bottom))
    width = max(longest(left) + longest(right) + 2 * _GRID, (columns + 1) * _GRID, 4 * _GRID)
    side_end, half_width = edge(width / 2, has_pins(left, right))
    first_column = -((columns - 1) // 2) * _GRID

    placed = []  # (pin, x, y, angle), with y pointing down as on the sheet
    for index, pin in enumerate(left):
        placed.append((pin, -side_end, first_row + index * _GRID, 0))
    for index, pin in enumerate(right):
        placed.append((pin, side_end, first_row + index * _GRID, 180))
    for index, pin in enumerate(top):
        placed.append((pin, first_column + index * _GRID, top_end, 270))
    for index, pin in enumerate(bottom):
        placed.append((pin, first_column + index * _GRID, bottom_end, 90))

    def mm(mils: float) -> str:
        return _number(mils * MIL)

    def at(x: float, y: float) -> str:
        return f"{mm(x)} {mm(-y)}"  # the file format has y pointing up

    font = "(effects (font (size 1.27 1.27))"
    hidden = f"{font} (hide yes))"

    def shown(y: float, crowded: bool) -> str:
        # The reference goes above the body and the value below it, from the left edge. Where
        # that edge carries pins, the text ends short of the first one instead, clear of its
        # number and of a wire that leaves it.
        if crowded:
            return f"(at {at(first_column - _GRID - 25, y)} 0) {font} (justify right))"
        return f"(at {at(-half_width, y)} 0) {font} (justify left))"

    reference_text = shown(body_top - 50, bool(top))
    value_text = shown(body_bottom + 50, bool(bottom))
    body = f"(start {at(-half_width, body_top)}) (end {at(half_width, body_bottom)})"
    lines = [
        f"(symbol {_quote(name)}",
        "\t\t(exclude_from_sim no)",
        "\t\t(in_bom yes)",
        "\t\t(on_board yes)",
        f'\t\t(property "Reference" {_quote(reference)} {reference_text})',
        f'\t\t(property "Value" {_quote(name)} {value_text})',
    ]
    for title, text in metadata.items():
        # KiCad keeps the keywords and the footprint filters as properties named ki_...; the
        # others are fields that every symbol has, empty or not.
        if text or not title.startswith("ki_"):
            lines.append(f"\t\t(property {_quote(title)} {_quote(text)} (at 0 0 0) {hidden})")
    lines += [
        f"\t\t(symbol {_quote(name + '_0_1')}",
        f"\t\t\t(rectangle {body} (stroke (width 0.254) (type default)) (fill (type background)))",
        "\t\t)",
        f"\t\t(symbol {_quote(name + '_1_1')}",
    ]

    def pin_text(kind: str, number: str, pin_name: str, place: str, more: str = "") -> str:
        return (
            f"\t\t\t(pin {PIN_TYPES[kind]} line {place} (name {_quote(pin_name)} {font})) "
            f"(number {_quote(number)} {font})){more})"
        )

    for pin, x, y, angle in placed:
        if not isinstance(pin, SymbolPin):
            continue
        # KiCad prints ~ for a pin without a name, and older file formats read it as that.
        pin_name = "" if pin.name == "~" else pin.name
        place = f"(at {at(x, y)} {angle}) (length {mm(length)})"
        alternates = "".join(
            f" (alternate {_quote(function)} {PIN_TYPES[kind]} line)"
            for function, kind in pin.alternates
        )
        lines.append(pin_text(pin.type, pin.number, pin_name, place, alternates))
        hidden_type = "passive" if pin.type in _SINGLE_TYPES else pin.type
        for number in pin.stacked:
            lines.append(pin_text(hidden_type, number, pin_name, f"{place} (hide yes)"))
    lines += ["\t\t)", "\t)"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Footprints


def write_footprint(
    project_file: Path,
    name: str,
    pads: list[Pad],
    *,
    body: tuple[float, float] | None = None,
    description: str = "",
    silk: bool = False,
) -> Path:
    """Write the footprint into the project's footprint library, replacing one of the same
    name, and register the library. Returns the footprint file.

    The courtyard is the box around the pads and the body plus 0.25 mm. The body outline goes on
    the fabrication layer. Nothing is drawn on the silkscreen unless ``silk`` asks for a pin-1
    dot.
    """
    library = footprint_library(project_file)
    library.mkdir(exist_ok=True)

    lefts = [pad.x - pad.width / 2 for pad in pads]
    rights = [pad.x + pad.width / 2 for pad in pads]
    tops = [pad.y - pad.height / 2 for pad in pads]
    bottoms = [pad.y + pad.height / 2 for pad in pads]
    if body is None:
        body_box = (min(lefts), min(tops), max(rights), max(bottoms))
    else:
        body_box = (-body[0] / 2, -body[1] / 2, body[0] / 2, body[1] / 2)
    margin = 0.25
    courtyard = (
        min(*lefts, body_box[0]) - margin,
        min(*tops, body_box[1]) - margin,
        max(*rights, body_box[2]) + margin,
        max(*bottoms, body_box[3]) + margin,
    )

    def box(corners, layer: str, width: float) -> list[str]:
        x1, y1, x2, y2 = (_number(value) for value in corners)
        sides = ((x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1))
        return [
            f"\t(fp_line (start {a} {b}) (end {c} {d}) "
            f'(stroke (width {width}) (type solid)) (layer "{layer}"))'
            for a, b, c, d in sides
        ]

    text = "(effects (font (size 1 1) (thickness 0.15)))"
    through_hole = any(pad.drill is not None for pad in pads)
    reference_at, value_at = _number(courtyard[1] - 1), _number(courtyard[3] + 1)
    lines = [
        f"(footprint {_quote(name)}",
        "\t(version 20240108)",
        '\t(generator "cuprum")',
        '\t(layer "F.Cu")',
        f"\t(descr {_quote(description)})",
        f"\t(attr {'through_hole' if through_hole else 'smd'})",
        f'\t(property "Reference" "REF**" (at 0 {reference_at} 0) (layer "F.SilkS") {text})',
        f'\t(property "Value" {_quote(name)} (at 0 {value_at} 0) (layer "F.Fab") {text})',
        *box(courtyard, "F.CrtYd", 0.05),
        *box(body_box, "F.Fab", 0.1),
    ]
    if silk:
        first = pads[0]
        # A dot just outside the courtyard, on the side of the first pad.
        x = courtyard[0] - 0.3 if first.x <= 0 else courtyard[2] + 0.3
        lines.append(
            f"\t(fp_circle (center {_number(x)} {_number(first.y)}) "
            f"(end {_number(x + 0.1)} {_number(first.y)}) "
            '(stroke (width 0.2) (type solid)) (fill solid) (layer "F.SilkS"))'
        )
    for pad in pads:
        side = "B" if pad.back else "F"
        if pad.drill is not None:
            kind, layers, drill = "thru_hole", '"*.Cu" "*.Mask"', f" (drill {_number(pad.drill)})"
        elif pad.paste is not None:
            kind, layers, drill = "smd", f'"{side}.Cu" "{side}.Mask"', ""
        else:
            kind, layers, drill = "smd", f'"{side}.Cu" "{side}.Paste" "{side}.Mask"', ""
        rounding = " (roundrect_rratio 0.25)" if pad.shape == "roundrect" else ""
        lines.append(
            f"\t(pad {_quote(pad.number)} {kind} {pad.shape} "
            f"(at {_number(pad.x)} {_number(pad.y)}) "
            f"(size {_number(pad.width)} {_number(pad.height)}){drill} "
            f"(layers {layers}){rounding})"
        )
        lines += _paste_windows(pad)
    lines.append(")")

    target = library / f"{name}.kicad_mod"
    target.write_text("\n".join(lines) + "\n")
    _register(project_file, "fp-lib-table", "fp_lib_table", library)
    return target


def import_footprint(project_file: Path, source: Path) -> Path:
    """Copy the footprint file ``source`` into the project's footprint library, replacing one
    of the same name, and register the library. Returns the new file."""
    library = footprint_library(project_file)
    library.mkdir(exist_ok=True)
    target = library / source.name
    shutil.copyfile(source, target)
    _register(project_file, "fp-lib-table", "fp_lib_table", library)
    return target


def _paste_windows(pad: Pad) -> list[str]:
    """The paste openings of a pad whose paste is split up, the way KiCad's own libraries do
    it: the pad itself stays off the paste layer, and pads without a number that exist on the
    paste layer alone make the openings."""
    if pad.paste is None or pad.paste[2] <= 0:
        return []
    columns, rows, share = pad.paste
    cell_x, cell_y = pad.width / columns, pad.height / rows
    # The same factor on both sides, so that the windows cover the share of the area asked for.
    width, height = cell_x * math.sqrt(share), cell_y * math.sqrt(share)
    layer = "B.Paste" if pad.back else "F.Paste"
    return [
        f'\t(pad "" smd roundrect '
        f"(at {_number(pad.x + (column - (columns - 1) / 2) * cell_x)} "
        f"{_number(pad.y + (row - (rows - 1) / 2) * cell_y)}) "
        f'(size {_number(width)} {_number(height)}) (layers "{layer}") (roundrect_rratio 0.25))'
        for row in range(rows)
        for column in range(columns)
    ]


# ---------------------------------------------------------------------------
# Files


def libraries_at(path: Path) -> list[Path]:
    """The library files at ``path``: a ``.kicad_sym`` file or a ``.pretty`` folder itself, or
    the ones directly inside a folder."""
    candidates = sorted(path.iterdir()) if path.is_dir() and path.suffix != ".pretty" else [path]
    return [
        candidate
        for candidate in candidates
        if (candidate.suffix == ".kicad_sym" and candidate.is_file())
        or (candidate.suffix == ".pretty" and candidate.is_dir())
    ]


def part_names(library: Path) -> list[str]:
    """The names of the symbols in a ``.kicad_sym`` file or of the footprints in a ``.pretty``
    folder."""
    if library.suffix == ".pretty":
        return sorted(file.stem for file in library.glob("*.kicad_mod"))
    return list(_symbols(library.read_text()))


def in_library_table(project_file: Path, table_name: str) -> bool:
    """Whether the project's library table, ``sym-lib-table`` or ``fp-lib-table``, lists the
    project's own library."""
    table = project_file.parent / table_name
    blocks = _children(table.read_text()) if table.exists() else []
    prefix = f"(lib (name {_quote(library_name(project_file))})"
    return any(block.startswith(prefix) for block in blocks)


def _register(project_file: Path, table_name: str, root: str, library: Path) -> None:
    """Make sure the project's library table lists the library, under the project's name."""
    if in_library_table(project_file, table_name):
        return
    table = project_file.parent / table_name
    entry = (
        f'(lib (name {_quote(library_name(project_file))})(type "KiCad")'
        f'(uri "${{KIPRJMOD}}/{library.name}")(options "")(descr ""))'
    )
    blocks = _children(table.read_text()) if table.exists() else []
    rows = [block for block in blocks if block.startswith("(lib ")] + [entry]
    table.write_text(f"({root}\n\t(version 7)\n" + "".join(f"\t{row}\n" for row in rows) + ")\n")


def _children(text: str) -> list[str]:
    """The lists directly inside the outermost list of an S-expression, as text."""
    children, depth, start, in_string, escaped = [], 0, None, False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
            if depth == 2:
                start = index
        elif char == ")":
            if depth == 2 and start is not None:
                children.append(text[start : index + 1])
            depth -= 1
    return children


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(text: str) -> str:
    """The text between the quotes of a string in a file, without its escapes."""
    return re.sub(r"\\(.)", r"\1", text)


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text
