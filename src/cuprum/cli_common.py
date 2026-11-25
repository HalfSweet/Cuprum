"""Pieces shared by the schematic and the board commands.

A command gathers what it has to say as plain data (dicts, lists, strings and numbers) and hands
it to :func:`emit`, which prints it as text or, with ``--json``, as it is. The text is always made
from that data, so the two cannot disagree.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from cuprum.export import PNG_DPI
from cuprum.schematic import pin_side, pin_type_name

JSON_HELP = (
    "print the result as one line of JSON instead of text. Lengths are numbers in the unit the "
    "text uses (mils; millimetres under 'pcb'), IDs are complete, a value that is not set is null"
)


class CliError(Exception):
    pass


def resolve_schematic(file: str | None, *, required: bool = True) -> Path | None:
    """The project's schematic file: the one given with -f (a board file is accepted too), or
    the only KiCad project in the current directory."""
    if file:
        return Path(file).expanduser().resolve().with_suffix(".kicad_sch")
    projects = sorted(Path.cwd().glob("*.kicad_pro"))
    if len(projects) == 1:
        return projects[0].with_suffix(".kicad_sch").resolve()
    schematics = sorted(Path.cwd().glob("*.kicad_sch"))
    if not projects and len(schematics) == 1:
        return schematics[0].resolve()
    if required:
        raise CliError("no single KiCad project in the current directory; pass the file with -f")
    return None


def render_directory(schematic: Path, out: str | None) -> Path:
    if out:
        directory = Path(out).expanduser().resolve()
    else:
        directory = Path(tempfile.gettempdir()) / "cuprum-render" / schematic.stem
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def dpi(text: str) -> int:
    """The type of a --dpi option: a resolution KiCad plots at."""
    lowest, highest = PNG_DPI
    if not (text.isdigit() and lowest <= int(text) <= highest):
        raise argparse.ArgumentTypeError(f"KiCad plots at {lowest} to {highest} dpi, not {text}")
    return int(text)


def natural(text: str):
    """Sort key that orders R2 before R10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def short_id(item) -> str:
    return item.id.value[:8]


UUID = re.compile(r"\b([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def shorten_ids(text: str) -> str:
    """Cut the UUIDs in a line of output down to the 8 characters that 'show' prints."""
    return UUID.sub(r"\1", text)


# ---------------------------------------------------------------------------
# Output


def emit(args, data: dict, lines) -> None:
    """Print what a command has to say. ``data`` holds all of it and ``lines`` turns it into the
    lines of the text output; with --json the data itself is printed, as one line of JSON."""
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    else:
        for line in lines(data):
            print(line)


def note(text: str) -> None:
    """A remark about what a command just did. It goes to stderr and changes nothing else."""
    sys.stdout.flush()
    print(f"note: {text}", file=sys.stderr)


def number(value: float | None, digits: int = 4) -> int | float | None:
    """A number for the output data: rounded, and a whole number as an int, so that JSON reads
    4000 rather than 4000.0."""
    if value is None:
        return None
    value = round(value, digits)
    return int(value) if value == int(value) else value


def mil(nm: float | None) -> int | float | None:
    """Nanometres as mils, the unit of the schematic commands."""
    return None if nm is None else number(nm / 25_400, 3)


def mm(nm: float | None) -> int | float | None:
    """Nanometres as millimetres, the unit of the board commands."""
    return None if nm is None else number(nm / 1e6)


def num(value: float | None) -> str:
    """A number of the output data as text; ``-`` when it is not set."""
    if value is None:
        return "-"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def xy(point: dict) -> str:
    """A position of the output data as text: ``4000,3000``."""
    return f"{num(point['x'])},{num(point['y'])}"


def section_lines(sections: dict[str, list[str]]) -> list[str]:
    """Titled groups of rows as text. A group without rows is left out."""
    lines = []
    for title, rows in sections.items():
        if rows:
            lines.append(f"{title}:")
            lines += [f"  {row}" for row in rows]
    return lines


def violation_data(violation, at) -> dict:
    """An ERC or DRC violation; ``at`` turns its position into numbers."""
    data = {
        "type": violation.type,
        "severity": violation.severity,
        "description": violation.description,
        "position": at(violation.position),
    }
    if hasattr(violation, "layer"):
        data["layer"] = violation.layer or None
    return {**data, "items": list(violation.items), "excluded": violation.excluded}


def violation_lines(data: dict) -> list[str]:
    lines = []
    for violation in data["violations"]:
        layer = f" {violation['layer']}" if violation.get("layer") else ""
        items = f" [{', '.join(violation['items'])}]" if violation["items"] else ""
        excluded = " (excluded)" if violation["excluded"] else ""
        line = (
            f"{violation['severity']} {violation['type']} @{xy(violation['position'])}{layer}: "
            f"{violation['description']}{items}{excluded}"
        )
        lines.append(shorten_ids(line))
    return lines


def area_data(xs: list[float], ys: list[float]) -> dict:
    """The rectangle an enlarged render shows."""
    return {
        "left": number(xs[0]),
        "top": number(ys[0]),
        "right": number(xs[1]),
        "bottom": number(ys[1]),
    }


def area_row(area: dict) -> str:
    return f"{area['left']:g},{area['top']:g} -> {area['right']:g},{area['bottom']:g}"


def pad_kind(pad: dict) -> str:
    """What a row of text says of a pad's kind: ``smd`` or ``thru``, and the copper layer of a
    pad that is on one other than the front."""
    layer = pad["layer"]
    return pad["type"] if layer in (None, "F.Cu") else f"{pad['type']}  {layer}"


def courtyard_data(left: int, top: int, right: int, bottom: int) -> dict:
    """The box around a footprint's courtyard, from nanometres."""
    return {
        "left": mm(left),
        "top": mm(top),
        "right": mm(right),
        "bottom": mm(bottom),
        "width": mm(right - left),
        "height": mm(bottom - top),
    }


def courtyard_row(box: dict) -> str:
    return (
        f"courtyard {num(box['left'])},{num(box['top'])} -> {num(box['right'])},"
        f"{num(box['bottom'])}  {num(box['width'])}x{num(box['height'])}"
    )


def symbol_definition_data(lib_id: str, symbol) -> dict:
    """A library symbol: its fields, then each pin's number, name, type, connection point
    relative to the symbol origin (unrotated, in mils) and side of the body, whether it is
    hidden, as the further pins of a stack are, and the other functions it can take. A pin's
    unit is null when the pin belongs to every unit of the symbol."""
    pins = []
    for child in symbol.items:
        if child.kind != "SchematicPin":
            continue
        if child.proto.HasField("body_style") and child.proto.body_style.style != 1:
            continue
        pin = child.item
        pins.append(
            {
                "unit": child.unit,
                "number": pin.number,
                "name": pin.name if pin.name not in ("", "~") else None,
                "type": pin_type_name(pin.electrical_type),
                "position": {"x": mil(pin.position.x), "y": mil(pin.position.y)},
                "side": pin_side(pin.orientation),
                "hidden": not pin.visible,
                "alternates": [
                    {"name": other.name, "type": pin_type_name(other.electrical_type)}
                    for other in pin.alternates
                ],
            }
        )
    pins.sort(key=lambda pin: (pin["unit"] or 0, natural(pin["number"])))
    return {
        "lib_id": lib_id,
        "reference_prefix": symbol.reference_field.text.value,
        "unit_count": symbol.unit_count,
        "description": symbol.description_field.text.value or None,
        "keywords": symbol.keywords or None,
        "footprint": symbol.footprint_field.text.value or None,
        "footprint_filters": list(symbol.footprint_filters),
        "datasheet": symbol.datasheet_field.text.value or None,
        "pins": pins,
    }


def symbol_definition_lines(data: dict) -> list[str]:
    lines = [f"{data['lib_id']}  ref={data['reference_prefix']}  units={data['unit_count']}"]
    filters = " ".join(data["footprint_filters"])
    for title, text in (
        ("description", data["description"]),
        ("keywords", data["keywords"]),
        ("footprint", data["footprint"]),
        ("footprint filters", filters),
        ("datasheet", data["datasheet"]),
    ):
        if text:
            lines.append(f"{title}: {text}")
    unit = object()
    for pin in data["pins"]:
        if data["unit_count"] > 1 and pin["unit"] != unit:
            unit = pin["unit"]
            lines.append("all units:" if unit is None else f"unit {unit}:")
        row = f"  {pin['number']:<4} {pin['name'] or '~':<12} {pin['type']:<14} "
        more = [f"@{xy(pin['position'])}", pin["side"], "hidden" if pin["hidden"] else None]
        if pin["alternates"]:
            others = " ".join(f"{other['name']}/{other['type']}" for other in pin["alternates"])
            more.append(f"alt {others}")
        lines.append(row + "  ".join(part for part in more if part))
    return lines
