"""Exports, done by a one-shot ``kicad-cli`` process that reads the file from disk.

The export jobs of the IPC API are deliberately not used. A headless API server loads the
document for its first export job and keeps reusing that copy, so every later export (netlist,
plot, ...) misses the changes made since. Until that is fixed upstream, exporting has to go
through the saved file, which means the document must be saved first.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from kipy.server import find_kicad_cli

_CONTENT_THRESHOLD = 16  # of 255, in the grey-scale difference from the background
_AREA_INCHES = 8  # how long the longer side of a plotted area comes out, at the given dpi
_MAX_PAGE_PIXELS = 80e6  # for the whole schematic page that an area is cut out of
_MIN_PAGE_MM = 30.0
_FAR_MM = 1000.0
_PAPER = re.compile(r'\(paper "([^"]+)"(?: ([\d.]+) ([\d.]+))?[^)]*\)')
_PAGES_MM = {
    "A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
    "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
    "D": (863.6, 558.8), "E": (1117.6, 863.6), "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4),
}  # fmt: skip

_EMPTY_DRAWING_SHEET = """(kicad_wks (version 20231118) (generator "cuprum")
  (setup (textsize 1.5 1.5) (linewidth 0.15) (textlinewidth 0.15)
    (left_margin 10) (right_margin 10) (top_margin 10) (bottom_margin 10)))
"""


def export_netlist(schematic: Path, output: Path) -> None:
    """Write the schematic's netlist (KiCad format) to ``output``."""
    _kicad_cli("sch", "export", "netlist", "--output", output, schematic)


def render_schematic(
    schematic: Path, directory: Path, *, dpi: int = 300, crop: bool = True
) -> list[Path]:
    """Plot every sheet of the schematic to a PNG in ``directory`` and return the files.

    kicad-cli plots whole pages; with ``crop`` each image is cut down to what is drawn on it.
    """
    with tempfile.TemporaryDirectory() as scratch:
        _kicad_cli(
            "sch", "export", "png", "--output", scratch, "--dpi", dpi,
            "--exclude-drawing-sheet", schematic,
        )  # fmt: skip
        files = _collect(Path(scratch), directory)
    if crop:
        for image in files:
            crop_to_content(image, margin=dpi // 5)
    return files


def render_board(board: Path, output: Path, *, layers: list[str], dpi: int = 300) -> Path:
    """Plot the given layers of the board, bottom-most first, into the single PNG ``output``.

    The drawing is scaled to fill the image and painted on the theme's background colour.
    """
    with tempfile.TemporaryDirectory() as scratch:
        _plot_board(board, output, Path(scratch), layers, dpi, 0)
    return output


def render_board_area(
    board: Path, output: Path, area, *, layers: list[str], dpi: int = 300
) -> float:
    """Plot one part of the board, (left, top, right, bottom) in millimetres, into ``output``
    and return the pixels per millimetre. The longer side comes out 8 inches long at ``dpi``.

    kicad-cli has no option for plotting part of a board. What it does, at any scale other than
    1, is centre the bounding box of everything on the board on the page. So a copy of the board
    gets two dots far outside it that put the middle of that box on the middle of the area, and
    a page as large as the area is at that scale. The plot of the copy is then the area alone.
    """
    left, top, right, bottom = area
    width, height = right - left, bottom - top
    scale = _AREA_INCHES * 25.4 / max(width, height)
    page = [max(side * scale, _MIN_PAGE_MM) for side in (width, height)]
    text = board.read_text()
    text, found = _PAPER.subn(f'(paper "User" {page[0]:.4f} {page[1]:.4f})', text, count=1)
    if not found:
        raise RuntimeError(f"{board.name} does not say what its page size is")
    layer = next((x for x in ("Cmts.User", "Dwgs.User") if f'"{x}"' in text), "Edge.Cuts")
    centre = (left + right) / 2, (top + bottom) / 2
    dots = "".join(
        f"\t(gr_line (start {x:.6f} {y:.6f}) (end {x:.6f} {y:.6f}) "
        f'(stroke (width 0.01) (type solid)) (layer "{layer}") (uuid "{uuid.uuid4()}"))\n'
        for x, y in (
            (centre[0] - _FAR_MM, centre[1] - _FAR_MM),
            (centre[0] + _FAR_MM, centre[1] + _FAR_MM),
        )
    )
    text = text[: text.rstrip().rfind(")")] + dots + ")\n"
    pixels_per_mm = scale * dpi / 25.4
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "area" / board.name
        copy.parent.mkdir()
        copy.write_text(text)
        project = board.with_suffix(".kicad_pro")
        if project.exists():
            shutil.copy(project, copy.parent)
        _plot_board(copy, output, Path(scratch), layers, dpi, f"{scale:.6f}")
    # The page is larger than the area when the area is very narrow.
    _crop(output, width * pixels_per_mm, height * pixels_per_mm)
    return pixels_per_mm


def render_schematic_area(
    schematic: Path,
    output: Path,
    area,
    *,
    page: str | None = None,
    page_file: Path | None = None,
    dpi: int = 300,
) -> float:
    """Plot one part of a schematic sheet, (left, top, right, bottom) in mils, into ``output``
    and return the pixels per mil.

    ``page`` is the page number of the sheet (the root sheet's when None) and ``page_file`` the
    file of that sheet. kicad-cli plots whole pages, so the page is plotted finer and the area
    cut out of it: fine enough for the longer side to come out 8 inches long at ``dpi``, unless
    the whole page would then be too large a picture to handle.
    """
    left, top, right, bottom = area
    paper = _PAPER.search((page_file or schematic).read_text())
    if paper and paper[2]:
        page_mm = float(paper[2]), float(paper[3])
    else:
        page_mm = _PAGES_MM.get(paper[1] if paper else "", _PAGES_MM["A0"])
    finest = math.sqrt(_MAX_PAGE_PIXELS / (page_mm[0] / 25.4 * page_mm[1] / 25.4))
    plot_dpi = int(min(_AREA_INCHES * dpi * 1000 / max(right - left, bottom - top), finest))
    with tempfile.TemporaryDirectory() as scratch:
        pages = ["--pages", page] if page else []
        _kicad_cli(
            "sch", "export", "png", "--output", scratch, "--dpi", plot_dpi,
            "--exclude-drawing-sheet", *pages, schematic,
        )  # fmt: skip
        (plotted,) = Path(scratch).glob("*.png")
        pixels_per_mil = plot_dpi / 1000
        _crop(
            plotted,
            (right - left) * pixels_per_mil,
            (bottom - top) * pixels_per_mil,
            corner=(left * pixels_per_mil, top * pixels_per_mil),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(plotted, output)
    return pixels_per_mil


def _plot_board(board: Path, output: Path, scratch: Path, layers: list[str], dpi, scale) -> None:
    # The PNG export always draws the page border and title block, which overlap the
    # auto-scaled board. An empty drawing sheet gets rid of them.
    drawing_sheet = scratch / "empty.kicad_wks"
    drawing_sheet.write_text(_EMPTY_DRAWING_SHEET)
    plots = scratch / "plots"
    # kicad-cli writes one file per layer in --layers, each with the --common-layers drawn
    # over it. Asking for the bottom layer alone, with the rest as common layers, gives one
    # combined image stacked in the order given.
    _kicad_cli(
        "pcb", "export", "png", "--output", plots, "--layers", layers[0],
        "--common-layers", ",".join(layers[1:]), "--drawing-sheet", drawing_sheet,
        "--bg-color", "--scale", scale, "--dpi", dpi, board,
    )  # fmt: skip
    (plotted,) = plots.glob("*.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(plotted, output)


def _crop(image: Path, width: float, height: float, *, corner=None) -> None:
    """Cut a plotted PNG in place down to a box of this size in pixels: the one with its top
    left at ``corner``, or the one in the middle of the image."""
    from PIL import Image  # imported here so only rendering pays for loading Pillow

    with Image.open(image) as picture:
        if corner is None:
            corner = (picture.width - width) / 2, (picture.height - height) / 2
        left, top = (max(0, round(value)) for value in corner)
        right = min(picture.width, round(corner[0] + width))
        bottom = min(picture.height, round(corner[1] + height))
        if right <= left or bottom <= top:
            raise RuntimeError("the area lies outside the page")
        if (left, top, right, bottom) != (0, 0, picture.width, picture.height):
            picture.crop((left, top, right, bottom)).save(image)


def crop_to_content(image: Path, margin: int) -> None:
    """Crop a plotted PNG in place to what is drawn on it, plus a margin in pixels.

    Everything that clearly differs from the background colour (taken from the top-left pixel)
    counts as drawn, so texts and symbol bodies are included whatever their size.
    """
    from PIL import Image, ImageChops  # imported here so only rendering pays for loading Pillow

    with Image.open(image) as picture:
        flat = picture.convert("RGB")
        background = Image.new("RGB", flat.size, flat.getpixel((0, 0)))
        # The plot is not perfectly flat: the last pixel column, for one, is off by a single
        # shade over the whole height. Only a visible difference counts as content.
        difference = ImageChops.difference(flat, background).convert("L")
        box = difference.point(lambda shade: 255 if shade > _CONTENT_THRESHOLD else 0).getbbox()
        if box is None:
            return
        left, top, right, bottom = box
        box = (
            max(0, left - margin),
            max(0, top - margin),
            min(flat.width, right + margin),
            min(flat.height, bottom + margin),
        )
        picture.crop(box).save(image)


def _collect(scratch: Path, directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for plotted in sorted(scratch.glob("*.png")):
        target = directory / plotted.name
        shutil.move(plotted, target)
        files.append(target)
    return files


def kicad_cli() -> str:
    """The path of the ``kicad-cli`` executable of the installed KiCad."""
    return find_kicad_cli()


def _kicad_cli(*arguments) -> None:
    command = [kicad_cli(), *(str(argument) for argument in arguments)]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"kicad-cli {' '.join(command[1:4])} failed: {detail}")
