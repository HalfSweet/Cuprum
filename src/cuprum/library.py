"""Project-local library items: symbols and footprints for parts the KiCad libraries lack.

These are written as KiCad library files rather than through the IPC API. In the current KiCad
the API can open an existing library symbol or footprint and edit its children, but it cannot
create one, name it or set its fields, and the library-table commands have no handler. So the
files are generated here, and the API is used afterwards to load them and read them back.

A project gets one symbol library, ``NAME.kicad_sym``, and one footprint library, ``NAME.pretty``,
both next to the project file and both registered in the project's library tables under the
project's name.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

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

_GRID = 100  # mil; pins sit on this grid
_PIN_LENGTH = 100
_CHAR_WIDTH = 50  # mil, a generous estimate for the default 50 mil text


@dataclass(frozen=True)
class SymbolPin:
    number: str
    name: str
    type: str
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
    pins: list[SymbolPin],
    *,
    reference: str = "U",
    footprint: str = "",
    description: str = "",
    datasheet: str = "",
) -> Path:
    """Add the symbol to the project's symbol library, replacing one of the same name, and
    register the library. Returns the library file."""
    library = symbol_library(project_file)
    blocks = _children(library.read_text()) if library.exists() else []
    blocks = [b for b in blocks if not b.startswith(f"(symbol {_quote(name)}")]
    blocks.append(_symbol(name, pins, reference, footprint, description, datasheet))
    kept = [b for b in blocks if b.startswith("(symbol ")]
    library.write_text(
        '(kicad_symbol_lib\n\t(version 20231120)\n\t(generator "cuprum")\n'
        + "".join(f"\t{block}\n" for block in kept)
        + ")\n"
    )
    _register(project_file, "sym-lib-table", "sym_lib_table", library)
    return library


def _symbol(
    name: str,
    pins: list[SymbolPin],
    reference: str,
    footprint: str,
    description: str,
    datasheet: str,
) -> str:
    by_side = {side: [pin for pin in pins if pin.side == side] for side in PIN_SIDES}
    left, right, top, bottom = (by_side[side] for side in PIN_SIDES)

    def longest(side_pins) -> int:
        return max((len(pin.name) for pin in side_pins), default=0) * _CHAR_WIDTH

    def up(mils: float) -> int:
        return math.ceil(mils / _GRID) * _GRID

    # Side pins run down from the top on the grid, with the symbol origin near the middle.
    rows = max(len(left), len(right), 1)
    first_row = -((rows - 1) // 2) * _GRID
    # Pins on the top and bottom carry their names inside the body, vertically.
    body_top = first_row - _GRID - (up(longest(top)) if top else 0)
    body_bottom = first_row + (rows - 1) * _GRID + _GRID + (up(longest(bottom)) if bottom else 0)
    columns = max(len(top), len(bottom))
    width = max(longest(left) + longest(right) + 2 * _GRID, (columns + 1) * _GRID, 4 * _GRID)
    half_width = up(width / 2)
    first_column = -((columns - 1) // 2) * _GRID

    placed = []  # (pin, x, y, angle), with y pointing down as on the sheet
    for index, pin in enumerate(left):
        placed.append((pin, -half_width - _PIN_LENGTH, first_row + index * _GRID, 0))
    for index, pin in enumerate(right):
        placed.append((pin, half_width + _PIN_LENGTH, first_row + index * _GRID, 180))
    for index, pin in enumerate(top):
        placed.append((pin, first_column + index * _GRID, body_top - _PIN_LENGTH, 270))
    for index, pin in enumerate(bottom):
        placed.append((pin, first_column + index * _GRID, body_bottom + _PIN_LENGTH, 90))

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
        f'\t\t(property "Footprint" {_quote(footprint)} (at 0 0 0) {hidden})',
        f'\t\t(property "Datasheet" {_quote(datasheet)} (at 0 0 0) {hidden})',
        f'\t\t(property "Description" {_quote(description)} (at 0 0 0) {hidden})',
        f"\t\t(symbol {_quote(name + '_0_1')}",
        f"\t\t\t(rectangle {body} (stroke (width 0.254) (type default)) (fill (type background)))",
        "\t\t)",
        f"\t\t(symbol {_quote(name + '_1_1')}",
    ]
    for pin, x, y, angle in placed:
        lines.append(
            f"\t\t\t(pin {PIN_TYPES[pin.type]} line (at {at(x, y)} {angle}) "
            f"(length {mm(_PIN_LENGTH)}) (name {_quote(pin.name)} {font})) "
            f"(number {_quote(pin.number)} {font})))"
        )
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
        if pad.drill is not None:
            kind, layers, drill = "thru_hole", '"*.Cu" "*.Mask"', f" (drill {_number(pad.drill)})"
        elif pad.paste is not None:
            kind, layers, drill = "smd", '"F.Cu" "F.Mask"', ""
        else:
            kind, layers, drill = "smd", '"F.Cu" "F.Paste" "F.Mask"', ""
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
    return [
        f'\t(pad "" smd roundrect '
        f"(at {_number(pad.x + (column - (columns - 1) / 2) * cell_x)} "
        f"{_number(pad.y + (row - (rows - 1) / 2) * cell_y)}) "
        f'(size {_number(width)} {_number(height)}) (layers "F.Paste") (roundrect_rratio 0.25))'
        for row in range(rows)
        for column in range(columns)
    ]


# ---------------------------------------------------------------------------
# Files


def _register(project_file: Path, table_name: str, root: str, library: Path) -> None:
    """Make sure the project's library table lists the library, under the project's name."""
    table = project_file.parent / table_name
    nickname = library_name(project_file)
    entry = (
        f'(lib (name {_quote(nickname)})(type "KiCad")'
        f'(uri "${{KIPRJMOD}}/{library.name}")(options "")(descr ""))'
    )
    blocks = _children(table.read_text()) if table.exists() else []
    if any(block.startswith(f"(lib (name {_quote(nickname)})") for block in blocks):
        return
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


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text
