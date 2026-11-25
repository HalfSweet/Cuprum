"""The ``cuprum pcb`` commands. Coordinates and sizes are in millimetres, angles in degrees."""

from __future__ import annotations

import math
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from itertools import pairwise

from kipy.board_rules import CustomRule, CustomRuleConstraint
from kipy.board_types import (
    BoardPolygon,
    BoardRectangle,
    BoardSegment,
    BoardText,
    Track,
    Via,
    Zone,
)
from kipy.errors import ApiError
from kipy.geometry import Angle, PolyLine, PolyLineNode, Vector2
from kipy.project import Project
from kipy.project_types import NetClass, NetClassAssignments, NetClassPatternAssignment
from kipy.proto.board.board_commands_pb2 import CustomRulesStatus
from kipy.proto.board.board_pb2 import BoardStackupDielectricType, BoardStackupLayerType
from kipy.proto.board.board_rules_pb2 import (
    CustomRuleConstraintType,
    CustomRuleLayerMode,
)
from kipy.proto.board.board_types_pb2 import (
    BoardLayer,
    IslandRemovalMode,
    PadType,
    ZoneConnectionStyle,
    ZoneType,
)
from kipy.proto.common.types import (
    DocumentSpecifier,
    MapMergeMode,
    MinOptMax,
    project_settings_pb2,
)
from kipy.util.board_layer import is_copper_layer

from cuprum.board import (
    format_mm,
    layer_from_name,
    layer_name,
    refill_zones,
    reset_stackup,
    run_drc,
    set_stackup,
    set_text_angles,
    stackup_names,
    sync_board,
    text_angles,
    update_footprints,
)
from cuprum.cli_common import (
    CliError,
    area_data,
    area_row,
    courtyard_data,
    courtyard_row,
    emit,
    mm,
    natural,
    num,
    number,
    render_directory,
    resolve_schematic,
    section_lines,
    short_id,
    violation_data,
    violation_lines,
    xy,
)
from cuprum.copper import Shape, gap, pad_shape, polygon_shapes, track_shapes, via_shape
from cuprum.export import render_board, render_board_area
from cuprum.routing import DIAGONAL_PLACES, bend, chamfer, point_in_polygon
from cuprum.session import connect

_NUMBER = r"-?\d+(?:\.\d+)?"
_XY = re.compile(rf"({_NUMBER}),({_NUMBER})")
_RELATIVE = re.compile(rf"@({_NUMBER}),({_NUMBER})")
_AXIS = re.compile(rf"([xy])=({_NUMBER})")
_WIDTH = re.compile(r"w=(\d+(?:\.\d+)?)")
_ARRAY = re.compile(r"([1-9]\d*)x([1-9]\d*)")
_DEFAULT_RENDER_LAYERS = "B.Cu,F.Cu,B.SilkS,F.SilkS,Edge.Cuts"
_THERMAL_SPOKE_MM = 0.5
_THERMAL_GAP_MM = 0.5
_MIN_WIDTH_NM = 250_000  # what KiCad gives a new zone
_KEEPOUT_KINDS = ("tracks", "vias", "copper", "pads", "footprints")
_RULE_CONSTRAINTS = {
    "clearance": CustomRuleConstraintType.CRCT_CLEARANCE,
    "hole_clearance": CustomRuleConstraintType.CRCT_HOLE_CLEARANCE,
    "hole_to_hole": CustomRuleConstraintType.CRCT_HOLE_TO_HOLE,
    "edge_clearance": CustomRuleConstraintType.CRCT_EDGE_CLEARANCE,
    "track_width": CustomRuleConstraintType.CRCT_TRACK_WIDTH,
    "annular_width": CustomRuleConstraintType.CRCT_ANNULAR_WIDTH,
    "silk_clearance": CustomRuleConstraintType.CRCT_SILK_CLEARANCE,
}
# The board-wide constraints of 'pcb rules': the option, where kicad-python keeps the value,
# and what it limits. They are what the board house can make, so DRC reports anything below.
_BOARD_CONSTRAINTS = {
    "min_clearance": ("constraints", "min_clearance", "copper to copper of another net"),
    "min_track": ("constraints", "min_track_width", "track width"),
    "min_connection": (
        "constraints",
        "min_connection_width",
        "width of a neck in the copper of one net, zone fills included",
    ),
    "min_via": ("constraints", "min_via_size", "via diameter"),
    "min_drill": ("constraints", "min_through_drill", "drilled hole diameter"),
    "min_annular": ("constraints", "min_via_annular_width", "annular ring around a hole"),
    "hole_clearance": ("constraints", "hole_clearance", "hole to copper of another net"),
    "hole_to_hole": ("constraints", "hole_to_hole_min", "between the edges of two holes"),
    "edge_clearance": ("constraints", "copper_edge_clearance", "copper to the board edge"),
    "silk_clearance": (
        "constraints",
        "silk_clearance",
        "silkscreen to other silkscreen and to the board edge",
    ),
    "min_text_height": ("constraints", "min_silk_text_height", "silkscreen text height"),
    "min_text_thickness": ("constraints", "min_silk_text_thickness", "silkscreen text stroke"),
    "mask_expansion": (
        "solder_mask_paste",
        "mask_expansion",
        "how far a solder mask opening extends beyond its pad, on each side",
    ),
    "mask_min_width": (
        "solder_mask_paste",
        "mask_min_width",
        "narrowest solder mask bridge; openings closer than this are merged",
    ),
    "mask_to_copper": (
        "solder_mask_paste",
        "mask_to_copper_clearance",
        "solder mask opening to copper of another net",
    ),
}
_RULE_LAYER_MODES = {
    "outer": CustomRuleLayerMode.CRLM_OUTER,
    "inner": CustomRuleLayerMode.CRLM_INNER,
}


@contextmanager
def _board(args):
    path = resolve_schematic(args.file).with_suffix(".kicad_pcb")
    if not path.exists():
        raise CliError(f"{path} does not exist; create it with 'cuprum pcb sync'")
    with connect(path) as kicad:
        yield kicad.get_board()


# ---------------------------------------------------------------------------
# Parsing


def _nm(millimetres: float) -> int:
    return round(millimetres * 1e6)


def _point(token: str) -> Vector2:
    match = _XY.fullmatch(token)
    if not match:
        raise CliError(f"bad position {token!r}: expected X,Y in millimetres")
    return Vector2.from_xy(_nm(float(match[1])), _nm(float(match[2])))


def _layer(name: str):
    try:
        return layer_from_name(name)
    except ValueError as error:
        raise CliError(str(error)) from None


def _copper_layer(name: str):
    layer = _layer(name)
    if not is_copper_layer(layer):
        raise CliError(f"{name} is not a copper layer")
    return layer


class _Positions:
    """Turns position tokens into board positions: ``X,Y``, ``REF.PAD`` and, relative to the
    position before, ``@DX,DY``, ``x=X`` and ``y=Y``."""

    def __init__(self, board):
        self.footprints = cache(board.get_footprints)

    def _candidates(self, token: str) -> list[tuple[Vector2, object]] | None:
        if _RELATIVE.fullmatch(token) or _AXIS.fullmatch(token):
            return None
        if _XY.fullmatch(token):
            return [(_point(token), None)]
        reference, dot, number = token.partition(".")
        if not (dot and number):
            raise CliError(
                f"bad position {token!r}: expected X,Y in millimetres, REF.PAD, or @DX,DY, x=X "
                "or y=Y relative to the position before"
            )
        footprints = [f for f in self.footprints() if _reference(f) == reference]
        if not footprints:
            raise CliError(f"no footprint with reference {reference}")
        pads = [p for f in footprints for p in f.definition.pads]
        matches = [(p.position, p) for p in pads if p.number == number]
        if not matches:
            known = ", ".join(sorted({p.number for p in pads if p.number}, key=natural))
            raise CliError(f"{reference} has no pad {number}; its pads are {known}")
        return matches

    @staticmethod
    def _from(previous: Vector2, token: str) -> Vector2:
        if match := _RELATIVE.fullmatch(token):
            dx, dy = _nm(float(match[1])), _nm(float(match[2]))
            return Vector2.from_xy(previous.x + dx, previous.y + dy)
        axis, value = _AXIS.fullmatch(token).groups()
        if axis == "x":
            return Vector2.from_xy(_nm(float(value)), previous.y)
        return Vector2.from_xy(previous.x, _nm(float(value)))

    def resolve(self, tokens: list[str]) -> list[tuple[Vector2, object]]:
        """Resolve the tokens to (position, pad or None). When several pads share a number (the
        tab of a SOT-223, say), the one nearest to the neighbouring point is taken."""
        options = [self._candidates(token) for token in tokens]
        resolved: list = []
        for index, (token, candidates) in enumerate(zip(tokens, options, strict=True)):
            previous = resolved[-1][0] if resolved else None
            if candidates is None:
                if previous is None:
                    raise CliError(f"{token} is relative to a position before it; there is none")
                resolved.append((self._from(previous, token), None))
            elif len(candidates) == 1:
                resolved.append(candidates[0])
            else:
                later = (o[0][0] for o in options[index + 1 :] if o is not None and len(o) == 1)
                anchor = previous if previous is not None else next(later, None)
                if anchor is None:
                    places = "; ".join(format_mm(position) for position, _pad in candidates)
                    raise CliError(f"{token} matches several pads ({places}); give coordinates")
                resolved.append(min(candidates, key=lambda c: _distance(c[0], anchor)))
        return resolved


def _distance(a: Vector2, b: Vector2) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _find_net(board, name: str):
    """The net called ``name``. A net named by a local label carries its sheet path (``/VIN``);
    the bare label name is accepted for it as long as that is unambiguous."""
    nets = [net for net in board.get_nets() if net.name]
    exact = [net for net in nets if net.name == name]
    matches = exact or [net for net in nets if net.name.rsplit("/", 1)[-1] == name]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(sorted((net.name for net in nets), key=natural))
    problem = "matches several nets" if matches else "is not a net"
    raise CliError(f"{name} {problem}; the nets are {known}")


def _xy(point: Vector2) -> tuple[int, int]:
    return point.x, point.y


def _copper_layers(board) -> list:
    """The board's copper layers, front to back."""
    layers = [x for x in board.get_enabled_layers() if is_copper_layer(x)]
    return sorted(layers, key=lambda x: (x == BoardLayer.BL_B_Cu, x))


def _pad_copper(board, footprints) -> dict[str, dict]:
    """The copper of every pad as shapes, by pad ID and then by layer."""
    copper: dict[str, dict] = {}
    odd: dict = {}
    for footprint in footprints:
        for pad in footprint.definition.pads:
            layers = [x for x in pad.padstack.layers if is_copper_layer(x)]
            shape = pad_shape(pad)
            if shape is not None:
                copper[pad.id.value] = {layer: [shape] for layer in layers}
            else:
                for layer in layers:
                    odd.setdefault(layer, []).append(pad)
    # A pad of any other form (chamfered, trapezoid, custom) is taken as the polygon KiCad
    # makes of it, in which curves are short straight pieces.
    for layer, pads in odd.items():
        polygons = board.get_pad_shapes_as_polygons(pads, layer)
        for pad, polygon in zip(pads, polygons, strict=True):
            if polygon is not None:
                copper.setdefault(pad.id.value, {})[layer] = polygon_shapes([polygon])
    return copper


class _Copper:
    """The copper on the board as it is before a command adds to it: which net a point is on,
    and which pads new copper runs into."""

    def __init__(self, board, footprints):
        self.pads = [(f, pad) for f in footprints for pad in f.definition.pads]
        self.pad_copper = _pad_copper(board, footprints)
        self.tracks = board.get_tracks()
        self.vias = board.get_vias()
        self.zones = board.get_zones()

    def _pad_gap(self, pad, layer, start, end) -> float:
        """The distance from a segment (a point, when both ends are the same) to the copper the
        pad has on this layer, or on any layer when it is None."""
        probe = Shape((start,) if start == end else (start, end))
        return min(
            (
                gap(probe, shape)[0]
                for key, shapes in self.pad_copper.get(pad.id.value, {}).items()
                if layer is None or key == layer
                for shape in shapes
            ),
            default=math.inf,
        )

    def _nets_at(self, point, layer, reach: float = 0.0):
        """The net names of the pads, vias and tracks that come within ``reach`` of the point on
        this layer (on any layer when it is None)."""
        probe = Shape((point,))
        for _footprint, pad in self.pads:
            if self._pad_gap(pad, layer, point, point) <= reach:
                yield pad.net.name
        for via in self.vias:
            if gap(probe, via_shape(via))[0] <= reach:
                yield via.net.name
        for track in self.tracks:
            if layer is not None and track.layer != layer:
                continue
            if any(gap(probe, shape)[0] <= reach for shape in track_shapes(track)):
                yield track.net.name

    def net_at(self, point) -> str | None:
        """The net of the copper that covers this point."""
        return next((name for name in self._nets_at(point, None) if name), None)

    def reaches(self, point, layer, net_name: str, width: int) -> bool:
        """Whether a track of this width that ends on this point touches copper of the net
        there, which is what makes KiCad count the two as connected."""
        if net_name in self._nets_at(point, layer, width / 2):
            return True
        for zone in self.zones:
            if zone.net is None or zone.net.name != net_name or layer not in zone.layers:
                continue
            corners = [_xy(node.point) for node in zone.outline.outline.nodes if node.has_point]
            if len(corners) >= 3 and point_in_polygon(point, corners):
                return True
        return False

    def foreign(self, start, end, width: int, layer, net_name: str) -> list[str]:
        """The pads, tracks and vias of other nets that a track of this width, or a via of this
        diameter when both ends are the same point and ``layer`` is None, lies on."""
        hits = []
        for footprint, pad in self.pads:
            if pad.net.name != net_name and self._pad_gap(pad, layer, start, end) < width / 2:
                net = f"net {pad.net.name}" if pad.net.name else "no net"
                hits.append(f"pad {_reference(footprint)}.{pad.number or '~'} ({net})")
        # KiCad would quietly move the new copper to the net it touches.
        probe = Shape((start,) if start == end else (start, end), width / 2)
        for track in self.tracks:
            if track.net.name == net_name or (layer is not None and track.layer != layer):
                continue
            if any(gap(probe, shape)[0] == 0 for shape in track_shapes(track)):
                hits.append(f"track {short_id(track)} (net {track.net.name})")
        for via in self.vias:
            if via.net.name != net_name and gap(probe, via_shape(via))[0] == 0:
                hits.append(f"via {short_id(via)} (net {via.net.name})")
        return hits


def _pick_net(board, copper: _Copper, given: str | None, resolved):
    """The net for new copper: the one asked for, else the net of a pad named in the command,
    else the net of the copper under the first or last point."""
    if given:
        return _find_net(board, given)
    for _position, pad in resolved:
        if pad is not None and pad.net.name:
            return _find_net(board, pad.net.name)
    for position, _pad in (resolved[0], resolved[-1]):
        name = copper.net_at(_xy(position))
        if name:
            return _find_net(board, name)
    raise CliError("cannot tell which net this belongs to; pass --net")


def _note(text: str) -> None:
    """A remark about what a command just did. It goes to stderr and changes nothing else."""
    sys.stdout.flush()
    print(f"note: {text}", file=sys.stderr)


def _project(board) -> Project:
    project = board.get_project()
    try:
        project.get_net_classes()
    except ApiError:
        # This KiCad build validates the project with its comparisons inverted: a request is
        # rejected exactly when its name or its path equals the open project's. Until that is
        # fixed, send a name and a path that identify the project without being equal to them.
        document = DocumentSpecifier()
        document.CopyFrom(board.document)
        document.project.name += ".kicad_pro"
        document.project.path = document.project.path.rstrip("/")
        project = Project(board.client, document)
    return project


def _netclass(board, net):
    return board.get_netclass_for_nets(net)[net.name]


def _other_side(layer):
    if layer == BoardLayer.BL_F_Cu:
        return BoardLayer.BL_B_Cu
    if layer == BoardLayer.BL_B_Cu:
        return BoardLayer.BL_F_Cu
    raise CliError("from an inner layer, say where the via leads: via:F.Cu")


def _new_via(position, net, netclass, size: float | None, drill: float | None):
    via = Via()
    via.position = Vector2.from_xy(*position)
    via.net = net
    via.diameter = _nm(size) if size is not None else netclass.via_diameter
    via.drill_diameter = _nm(drill) if drill is not None else netclass.via_drill
    return via


def _polyline(points: list[Vector2]) -> PolyLine:
    outline = PolyLine()
    for point in points:
        outline.append(PolyLineNode.from_point(point))
    outline.closed = True
    return outline


def _corners(tokens: list[str]) -> list[Vector2]:
    """Polygon corners; two points are taken as opposite corners of a rectangle."""
    points = [_point(token) for token in tokens]
    if len(points) == 2:
        (x1, y1), (x2, y2) = ((p.x, p.y) for p in points)
        points = [Vector2.from_xy(x, y) for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]
    if len(points) < 3:
        raise CliError("give two opposite corners of a rectangle, or the corners of a polygon")
    return points


# ---------------------------------------------------------------------------
# Output


def _reference(footprint) -> str:
    return footprint.reference_field.text.value


def _find_footprint(board, reference: str):
    matches = [f for f in board.get_footprints() if _reference(f) == reference]
    if not matches:
        raise CliError(f"no footprint with reference {reference}")
    if len(matches) > 1:
        raise CliError(f"several footprints have the reference {reference}")
    return matches[0]


def _select_footprints(board, references: list[str], everything: bool, exclude: list[str]):
    """The footprints a command applies to: the ones named, or all of them, less the excluded."""
    footprints = board.get_footprints()
    known = {_reference(f) for f in footprints}
    for reference in [*references, *exclude]:
        if reference not in known:
            raise CliError(f"no footprint with reference {reference}")
    if everything == bool(references):
        raise CliError("give references, or --all for every footprint")
    chosen = [
        f
        for f in footprints
        if (everything or _reference(f) in references) and _reference(f) not in exclude
    ]
    return sorted(chosen, key=lambda f: natural(_reference(f)))


def _at(position) -> dict:
    return {"x": mm(position.x), "y": mm(position.y)}


def _pad_data(pad) -> dict:
    copper = pad.padstack.copper_layers
    width = height = None
    if copper:
        width, height = copper[0].size.x, copper[0].size.y
        if round(pad.padstack.angle.degrees) % 180 == 90:
            width, height = height, width
    return {
        "number": pad.number or None,
        "net": pad.net.name or None,
        "position": _at(pad.position),
        "width": mm(width),
        "height": mm(height),
        "type": {PadType.PT_PTH: "thru", PadType.PT_SMD: "smd"}.get(pad.pad_type, "other"),
    }


def _pad_row(data: dict) -> str:
    parts = [f"{data['number'] or '~':<4} {data['net'] or '~':<20} @{xy(data['position'])}"]
    if data["width"] is not None:
        parts.append(f"{num(data['width'])}x{num(data['height'])}")
    parts.append(data["type"])
    return "  ".join(parts)


def _footprint_data(footprint, *, detail: bool = False) -> dict:
    """A placed footprint; with ``detail`` also its courtyard, its texts and its pads."""
    data = {
        "reference": _reference(footprint),
        "footprint": str(footprint.definition.id),
        "value": footprint.value_field.text.value or None,
        "position": _at(footprint.position),
        "rotation": number(footprint.orientation.degrees),
        "side": "back" if footprint.layer == BoardLayer.BL_B_Cu else "front",
    }
    if detail:
        box = _courtyard(footprint)
        data["courtyard"] = None if box is None else courtyard_data(*box)
        data["texts"] = [
            _field_data(getattr(footprint, f"{name}_field"), name)
            for name in ("reference", "value")
        ]
        # A pad that is on no copper layer is an opening in the paste stencil or the mask.
        data["pads"] = [
            _pad_data(pad)
            for pad in sorted(footprint.definition.pads, key=lambda p: natural(p.number))
            if any(is_copper_layer(x) for x in pad.padstack.layers)
        ]
    return data


def _footprint_lines(data: dict, *, courtyard: bool = False, pads: bool = False) -> list[str]:
    parts = [data["reference"], data["footprint"]]
    if data["value"]:
        parts.append(data["value"])
    parts.append("@" + xy(data["position"]))
    if data["rotation"]:
        parts.append(f"rot{data['rotation']:g}")
    if data["side"] == "back":
        parts.append("back")
    lines = ["  ".join(parts)]
    if courtyard and data["courtyard"] is not None:
        lines.append("  " + courtyard_row(data["courtyard"]))
    if pads:
        lines.append("  " + _field_row(data["texts"][0]))
        lines += ["  " + _pad_row(pad) for pad in data["pads"]]
    return lines


def _footprint_listing(*, pads: bool):
    """The text of a command that answers with footprints in detail."""
    return lambda data: [
        line
        for footprint in data["footprints"]
        for line in _footprint_lines(footprint, courtyard=True, pads=pads)
    ]


def _courtyard(footprint) -> tuple[int, int, int, int] | None:
    """The box (left, top, right, bottom) around the footprint's courtyard, the area that other
    parts have to stay out of."""
    layers = (BoardLayer.BL_F_CrtYd, BoardLayer.BL_B_CrtYd)
    boxes = [s.bounding_box() for s in footprint.definition.shapes if s.layer in layers]
    if not boxes:
        return None
    # The box of a shape in a turned footprint can come with a negative size.
    xs = [x for box in boxes for x in (box.pos.x, box.pos.x + box.size.x)]
    ys = [y for box in boxes for y in (box.pos.y, box.pos.y + box.size.y)]
    return min(xs), min(ys), max(xs), max(ys)


def _field_data(field, name: str) -> dict:
    return {
        "name": name,
        "layer": layer_name(field.text.layer),
        "position": _at(field.text.position),
        "visible": field.visible,
    }


def _field_row(data: dict) -> str:
    hidden = "" if data["visible"] else "  hidden"
    return f"{data['name']} text  {data['layer']}  @{xy(data['position'])}{hidden}"


def _shape_data(shape) -> dict:
    """A drawn shape: a rectangle by two opposite corners, a line by its ends, a polygon by its
    corners. Other kinds come without points."""
    kind, points, radius = type(shape).__name__.removeprefix("Board").lower(), [], None
    if isinstance(shape, BoardRectangle):
        kind, points = "rect", [shape.top_left, shape.bottom_right]
        radius = mm(shape.proto.shape.rectangle.corner_radius.value_nm) or None
    elif isinstance(shape, BoardSegment):
        kind, points = "line", [shape.start, shape.end]
    elif isinstance(shape, BoardPolygon):
        points = [n.point for p in shape.polygons for n in p.outline.nodes if n.has_point]
    return {
        "id": shape.id.value,
        "kind": kind,
        "points": [_at(point) for point in points],
        "corner_radius": radius,
    }


def _shape_row(data: dict) -> str:
    points = [xy(point) for point in data["points"]]
    if data["kind"] in ("rect", "line"):
        geometry = f"{data['kind']} {points[0]} -> {points[1]}"
        if data["corner_radius"]:
            geometry += f"  radius {num(data['corner_radius'])}"
    else:
        geometry = " ".join([data["kind"], *points])
    return f"{data['id'][:8]}  {geometry}"


def _track_data(track) -> dict:
    return {
        "id": track.id.value,
        "net": track.net.name or None,
        "layer": layer_name(track.layer),
        "width": mm(track.width),
        "start": _at(track.start),
        "end": _at(track.end),
        "arc": not isinstance(track, Track),
    }


def _track_row(data: dict) -> str:
    kind = "arc " if data["arc"] else ""
    return (
        f"{data['id'][:8]}  {data['net'] or '~'}  {data['layer']}  "
        f"w{num(data['width'])}  {kind}{xy(data['start'])} -> {xy(data['end'])}"
    )


def _via_data(via) -> dict:
    return {
        "id": via.id.value,
        "net": via.net.name or None,
        "position": _at(via.position),
        "diameter": mm(via.diameter),
        "drill": mm(via.drill_diameter),
    }


def _via_row(data: dict) -> str:
    return (
        f"{data['id'][:8]}  {data['net'] or '~'}  @{xy(data['position'])}  "
        f"{num(data['diameter'])}/{num(data['drill'])}"
    )


def _area_data(zone) -> dict:
    """What a copper zone and a keepout area have in common, less the ID."""
    return {
        "name": zone.name or None,
        "layers": [layer_name(layer) for layer in zone.layers],
        "corners": [_at(node.point) for node in zone.outline.outline.nodes if node.has_point],
    }


def _zone_data(zone) -> dict:
    connection = zone.connection
    style = connection.zone_connection if connection is not None else None
    styles = {
        ZoneConnectionStyle.ZCS_FULL: "solid",
        ZoneConnectionStyle.ZCS_NONE: "none",
        ZoneConnectionStyle.ZCS_INHERITED: "inherited",
    }
    thermal = style is not None and style not in styles
    islands = {IslandRemovalMode.IRM_NEVER: "keep", IslandRemovalMode.IRM_AREA: "remove_below_area"}
    by_area = zone.island_mode == IslandRemovalMode.IRM_AREA
    return {
        "id": zone.id.value,
        "net": (zone.net.name or None) if zone.net is not None else None,
        **_area_data(zone),
        "filled": zone.filled,
        "priority": zone.priority,
        "clearance": mm(zone.clearance) or None,
        "min_width": mm(zone.min_thickness),
        "pad_connection": "thermal" if thermal else styles.get(style),
        "thermal_gap": mm(connection.thermal_spokes.gap) if thermal else None,
        "spoke_width": mm(connection.thermal_spokes.width) if thermal else None,
        "islands": islands.get(zone.island_mode, "remove"),
        "min_island_area": number(zone.min_island_area / 1e12, 6) if by_area else None,
    }


def _zone_row(data: dict) -> str:
    """Only the settings that differ from what 'pcb zone' gives a new zone are spelled out."""
    parts = [
        data["id"][:8],
        data["net"] or "~",
        ",".join(data["layers"]),
        f"{len(data['corners'])} corners",
        "filled" if data["filled"] else "unfilled",
    ]
    if data["priority"]:
        parts.append(f"priority {data['priority']}")
    if data["clearance"]:
        parts.append(f"clearance {num(data['clearance'])}")
    if data["min_width"] != mm(_MIN_WIDTH_NM):
        parts.append(f"min-width {num(data['min_width'])}")
    if data["pad_connection"] == "solid":
        parts.append("solid")
    elif data["pad_connection"] == "none":
        parts.append("pads not connected")
    elif data["pad_connection"] == "inherited":
        parts.append("pad connection as the pads say")
    elif (data["thermal_gap"], data["spoke_width"]) not in (
        (None, None),
        (_THERMAL_GAP_MM, _THERMAL_SPOKE_MM),
    ):
        parts.append(f"thermal gap {num(data['thermal_gap'])} spoke {num(data['spoke_width'])}")
    if data["islands"] == "keep":
        parts.append("islands kept")
    elif data["islands"] == "remove_below_area":
        parts.append(f"islands under {data['min_island_area']:g} mm2 removed")
    if data["name"]:
        parts.append(data["name"])
    return "  ".join(parts)


def _keepout_data(zone) -> dict:
    settings = zone.rule_area_settings
    banned = [kind for kind in _KEEPOUT_KINDS if getattr(settings, f"keepout_{kind}")]
    return {"id": zone.id.value, **_area_data(zone), "keeps_out": banned}


def _keepout_row(data: dict) -> str:
    name = f"  {data['name']}" if data["name"] else ""
    return (
        f"{data['id'][:8]}  keepout  {','.join(data['layers'])}  {len(data['corners'])} corners  "
        f"no {','.join(data['keeps_out']) or 'restriction'}{name}"
    )


def _find_zone(board, target: str, *, keepout: bool):
    kind = "keepout" if keepout else "zone"
    zones = [zone for zone in board.get_zones() if zone.is_rule_area() == keepout]
    matches = [zone for zone in zones if zone.id.value.startswith(target)]
    if len(matches) != 1:
        problem = "matches several" if matches else "is not the ID of a"
        raise CliError(f"{target} {problem} {kind}; 'cuprum pcb show' lists them")
    return matches[0]


@dataclass
class _Piece:
    """One piece of copper: a pad, a track, a via or the fill of a zone, with its net and its
    shape on each layer."""

    kind: str
    key: str
    """The object's ID, or for a pad REF.NUMBER."""
    net: str
    shapes: dict
    reference: str = ""
    width: int | None = None
    """Of a track."""


def _piece_data(piece: _Piece) -> dict:
    is_pad = piece.kind == "pad"
    return {
        "kind": piece.kind,
        "id": None if is_pad else piece.key,
        "pad": piece.key if is_pad else None,
        "net": piece.net or None,
        "width": mm(piece.width),
    }


def _piece_row(data: dict) -> str:
    name = data["pad"] if data["kind"] == "pad" else data["id"][:8]
    width = "" if data["width"] is None else f" w{num(data['width'])}"
    return f"{data['kind']} {name}{width} ({data['net'] or 'no net'})"


def _pieces(board, footprints=None) -> list[_Piece]:
    """All the copper on the board: pads, tracks, vias and filled zones."""
    footprints = board.get_footprints() if footprints is None else footprints
    layers = _copper_layers(board)
    pad_copper = _pad_copper(board, footprints)
    pieces = []
    for footprint in footprints:
        reference = _reference(footprint)
        for pad in footprint.definition.pads:
            shapes = pad_copper.get(pad.id.value)
            if shapes:
                key = f"{reference}.{pad.number}"
                pieces.append(_Piece("pad", key, pad.net.name, shapes, reference))
    for track in board.get_tracks():
        shapes = {track.layer: track_shapes(track)}
        pieces.append(_Piece("track", track.id.value, track.net.name, shapes, width=track.width))
    for via in board.get_vias():
        # The padstack names the two layers the via runs between.
        ends = [layers.index(x) for x in via.padstack.layers if x in layers]
        span = layers[min(ends) : max(ends) + 1] if ends else layers
        shapes = {layer: [via_shape(via)] for layer in span}
        pieces.append(_Piece("via", via.id.value, via.net.name, shapes))
    for zone in board.get_zones():
        if zone.is_rule_area():
            continue
        shapes = {layer: polygon_shapes(fill) for layer, fill in zone.filled_polygons.items()}
        net = zone.net.name if zone.net is not None else ""
        pieces.append(_Piece("zone", zone.id.value, net, shapes))
    return pieces


def _closest(first: list[_Piece], second: list[_Piece], layer_a, layer_b):
    """The smallest gap between the two groups of copper on the given layers, as (distance,
    point on the first, point on the second, piece of the first, piece of the second)."""
    best = None
    for a in first:
        for b in second:
            for shape_a in a.shapes.get(layer_a, []):
                for shape_b in b.shapes.get(layer_b, []):
                    distance, p, q = gap(shape_a, shape_b)
                    if best is None or distance < best[0]:
                        best = (distance, p, q, a, b)
    return best


def _text_data(text) -> dict:
    return {
        "id": text.id.value,
        "layer": layer_name(text.layer),
        "position": _at(text.position),
        "rotation": number(text.attributes.angle),
        "text": text.value,
    }


def _text_row(data: dict) -> str:
    rotation = f"  rot{data['rotation']:g}" if data["rotation"] else ""
    return f"{data['id'][:8]}  {data['layer']}  @{xy(data['position'])}{rotation}  {data['text']!r}"


def _edge_shapes(board):
    return [s for s in board.get_shapes() if s.layer == BoardLayer.BL_Edge_Cuts]


def _board_contents(data: dict) -> list[str]:
    """The sections of 'pcb show', whichever of them the data has."""
    rows = {
        "footprints": ("footprints", lambda footprint: _footprint_lines(footprint)[0]),
        "pads": ("pads", lambda pad: f"{pad['reference']}.{_pad_row(pad)}"),
        "outline": ("outline", _shape_row),
        "tracks": ("tracks", _track_row),
        "vias": ("vias", _via_row),
        "zones": ("zones", _zone_row),
        "keepouts": ("keepouts", _keepout_row),
        "texts": ("text", _text_row),
    }
    return section_lines(
        {
            title: [row(item) for item in data[key]]
            for key, (title, row) in rows.items()
            if key in data
        }
    )


def _emit_copper(args, created) -> None:
    """Print the tracks and vias a command made. The text has them in the order they were made,
    which is the order along the path."""
    rows = [
        ("via", _via_data(item)) if isinstance(item, Via) else ("track", _track_data(item))
        for item in created
    ]
    data = {
        "tracks": [row for kind, row in rows if kind == "track"],
        "vias": [row for kind, row in rows if kind == "via"],
    }
    emit(
        args,
        data,
        lambda _data: [
            f"{kind} {_via_row(row) if kind == 'via' else _track_row(row)}" for kind, row in rows
        ],
    )


# ---------------------------------------------------------------------------
# Commands


def _cmd_sync(args):
    schematic = resolve_schematic(args.file)
    if not schematic.exists():
        raise CliError(f"{schematic} does not exist")
    result, changes = sync_board(
        schematic, delete_extra=args.delete_extra, by_reference=args.by_reference
    )
    wordy = args.verbose or result.warning_count or result.error_count
    data = {
        "new_footprints": result.new_footprint_count,
        "warnings": result.warning_count,
        "errors": result.error_count,
        **changes,
        "report": result.report.strip() if wordy else None,
    }
    emit(args, data, _sync_lines)


def _sync_lines(data: dict) -> list[str]:
    counts = (data["new_footprints"], data["warnings"], data["errors"])
    lines = ["{} new footprints, {} warnings, {} errors".format(*counts)]
    for title in ("added", "removed"):
        if data[title]:
            lines.append(f"{title}: {' '.join(data[title])}")
    for footprint in data["changed"]:
        details = [
            f"{change['property']} {change['before']} -> {change['after']}"
            if change["pad"] is None
            else f"pad {change['pad']} {change['before'] or '~'} -> {change['after'] or '~'}"
            for change in footprint["changes"]
        ]
        lines.append(f"changed: {footprint['reference']}  {', '.join(details)}")
    if data["report"] is not None:
        lines.append(data["report"])
    return lines


def _cmd_show(args):
    with _board(args) as board:
        footprints = sorted(board.get_footprints(), key=lambda f: natural(_reference(f)))
        if args.references:
            chosen = []
            for reference in args.references:
                matches = [f for f in footprints if _reference(f) == reference]
                if not matches:
                    raise CliError(f"no footprint with reference {reference}")
                chosen += matches
            data = {"footprints": [_footprint_data(f, detail=True) for f in chosen]}
            emit(args, data, _footprint_listing(pads=True))
            return

        net_name = _find_net(board, args.net).name if args.net is not None else None

        def on_net(item) -> bool:
            return net_name is None or (item.net is not None and item.net.name == net_name)

        copper = {
            "tracks": [_track_data(t) for t in board.get_tracks() if on_net(t)],
            "vias": [_via_data(v) for v in board.get_vias() if on_net(v)],
        }
        areas = board.get_zones()
        zones = [_zone_data(z) for z in areas if not z.is_rule_area() and on_net(z)]
        if net_name is not None:
            pads = [
                {"reference": _reference(f), **_pad_data(p)}
                for f in footprints
                for p in f.definition.pads
                if p.net.name == net_name
            ]
            emit(args, {"net": net_name, "pads": pads, **copper, "zones": zones}, _board_contents)
            return

        data = {
            "footprints": [_footprint_data(f) for f in footprints],
            "outline": [_shape_data(s) for s in _edge_shapes(board)],
            **copper,
            "zones": zones,
            "keepouts": [_keepout_data(z) for z in areas if z.is_rule_area()],
            "texts": [_text_data(t) for t in board.get_text()],
        }
        emit(args, data, _board_contents)


def _cmd_move(args):
    if args.at is None and args.rot is None and args.side is None:
        raise CliError("nothing to do: give a position, --rot or --side")
    with _board(args) as board:
        footprint = _find_footprint(board, args.reference)
        was = [(key, item.net.name) for key, item in _routed(board).items()]
        if args.side is not None:
            on_back = footprint.layer == BoardLayer.BL_B_Cu
            if on_back != (args.side == "back"):
                board.flip_items(footprint)
                footprint = board.get_items_by_id(footprint.id)[0]
        # Position and rotation go last, so the result does not depend on how the flip moved
        # or turned the footprint.
        turned = {}
        if args.at is not None:
            footprint.position = _point(args.at)
        if args.rot is not None:
            turned[footprint.id.value] = args.rot - footprint.orientation.degrees
            footprint.orientation = Angle.from_degrees(args.rot)
        if args.at is not None or args.rot is not None:
            footprint = update_footprints(board, [footprint], turned=turned)[0]
        board.save()
        data = {"footprints": [_footprint_data(footprint, detail=True)]}
        emit(args, data, _footprint_listing(pads=args.pads))
        for remark in _renetted(was, _routed(board)):
            _note(remark)


def _cmd_add(args):
    with _board(args) as board:
        footprint = board.place_footprint_from_library(
            args.lib_id,
            _point(args.at),
            orientation=Angle.from_degrees(args.rot),
            layer=BoardLayer.BL_B_Cu if args.side == "back" else BoardLayer.BL_F_Cu,
        )
        footprint.reference_field.text.value = args.ref
        # Without this KiCad treats the footprint as a leftover whose symbol was deleted.
        footprint.attributes.not_in_schematic = True
        footprint = update_footprints(board, [footprint])[0]
        board.save()
        data = {"footprints": [_footprint_data(footprint, detail=True)]}
        emit(args, data, _footprint_listing(pads=True))


def _cmd_outline(args):
    if args.radius is not None and len(args.corners) != 2:
        raise CliError("--radius only applies to a rectangle given by two corners")
    with _board(args) as board:
        corners = _corners(args.corners)
        if args.radius is not None:
            shorter = min(abs(corners[2].x - corners[0].x), abs(corners[2].y - corners[0].y))
            if not 0 < _nm(args.radius) <= shorter // 2:
                raise CliError("--radius must be positive and at most half the shorter side")
        existing = _edge_shapes(board)
        if existing and not args.replace:
            raise CliError("the board already has an outline; pass --replace to redraw it")
        if existing:
            board.remove_items(existing)
        if len(args.corners) == 2:
            shape = BoardRectangle()
            shape.top_left, shape.bottom_right = corners[0], corners[2]
            if args.radius is not None:
                # kicad-python has no property for it yet, but KiCad's rectangle carries a
                # corner radius, and on Edge.Cuts it is a valid rounded outline.
                shape.proto.shape.rectangle.corner_radius.value_nm = _nm(args.radius)
        else:
            shape = BoardPolygon.from_outline(_polyline(corners))
        shape.layer = BoardLayer.BL_Edge_Cuts
        created = board.create_items(shape)
        board.save()
        emit(
            args,
            {"outline": [_shape_data(created[0])]},
            lambda data: [f"outline {_shape_row(shape)}" for shape in data["outline"]],
        )


def _path_steps(tokens: list[str]) -> list[tuple[str, object]]:
    """Split a track path into its steps: ("point", token), ("via", layer name or None) and
    ("width", nanometres)."""
    steps: list[tuple[str, object]] = []
    for token in tokens:
        if token == "via" or token.startswith("via:"):
            steps.append(("via", token.partition(":")[2] or None))
        elif match := _WIDTH.fullmatch(token):
            steps.append(("width", _nm(float(match[1]))))
        else:
            steps.append(("point", token))
    return steps


def _merge_straight(points: list, widths: list) -> tuple[list, list]:
    """Join consecutive runs that continue in the same direction at the same width."""
    points, widths = list(points), list(widths)
    index = 1
    while index < len(points) - 1:
        (x0, y0), (x1, y1), (x2, y2) = points[index - 1 : index + 2]
        ax, ay, bx, by = x1 - x0, y1 - y0, x2 - x1, y2 - y1
        if widths[index - 1] == widths[index] and ax * by == ay * bx and ax * bx + ay * by > 0:
            del points[index], widths[index]
        else:
            index += 1
    return points, widths


def _cmd_track(args):
    steps = _path_steps(args.points)
    point_tokens = [token for kind, token in steps if kind == "point"]
    first_kind = next((kind for kind, _ in steps if kind != "width"), None)
    if first_kind != "point":
        raise CliError("a track starts at a position; use 'cuprum pcb via' for a lone via")
    with _board(args) as board:
        positions = _Positions(board)
        resolved = positions.resolve(point_tokens)
        copper = _Copper(board, positions.footprints())
        net = _pick_net(board, copper, args.net, resolved)
        netclass = _netclass(board, net)
        width = _nm(args.width) if args.width is not None else netclass.track_width

        if args.layer is not None:
            layer = _copper_layer(args.layer)
        else:
            # An SMD pad sits on one copper layer; start there. Otherwise start on the front.
            first_pad = resolved[0][1]
            layers = (
                [x for x in first_pad.padstack.layers if is_copper_layer(x)] if first_pad else []
            )
            layer = layers[0] if len(layers) == 1 else BoardLayer.BL_F_Cu

        # A via ends the run on one layer and starts the next one at the same point.
        runs = [{"layer": layer, "points": [], "widths": [], "via": False}]
        places = iter(_xy(position) for position, _pad in resolved)
        for kind, value in steps:
            run = runs[-1]
            if kind == "width":
                width = value
            elif kind == "via":
                run["via"] = True
                layer = _copper_layer(value) if value else _other_side(run["layer"])
                runs.append(
                    {"layer": layer, "points": run["points"][-1:], "widths": [], "via": False}
                )
            else:
                point = next(places)
                if run["points"] and run["points"][-1] != point:
                    run["widths"].append(width)
                if not run["points"] or run["points"][-1] != point:
                    run["points"].append(point)

        items, sharp = [], []
        for run in runs:
            points, widths = run["points"], run["widths"]
            if not args.exact and widths:
                bent, bent_widths = [points[0]], []
                for (start, end), leg_width in zip(pairwise(points), widths, strict=True):
                    leg = bend(start, end, args.diagonal)
                    bent += leg[1:]
                    bent_widths += [leg_width] * (len(leg) - 1)
                points, widths = _merge_straight(bent, bent_widths)
                if args.chamfer is None or args.chamfer > 0:
                    size = None if args.chamfer is None else _nm(args.chamfer)
                    points, widths = chamfer(points, widths, size)
                for before, corner, after in zip(points, points[1:], points[2:], strict=False):
                    turn = (corner[0] - before[0]) * (after[0] - corner[0])
                    turn += (corner[1] - before[1]) * (after[1] - corner[1])
                    if turn < 0:
                        sharp.append(corner)
            for (start, end), leg_width in zip(pairwise(points), widths, strict=True):
                track = Track()
                track.start, track.end = Vector2.from_xy(*start), Vector2.from_xy(*end)
                track.layer, track.width, track.net = run["layer"], leg_width, net
                items.append(track)
            if run["via"]:
                items.append(_new_via(points[-1], net, netclass, args.via_size, args.via_drill))
        if not any(isinstance(item, Track) for item in items):
            raise CliError("need at least two distinct points")

        created = board.create_items(items)
        board.save()
        created, moved = _settled(board, copper, created, net.name)
        _emit_copper(args, created)

        _note_foreign(copper, created, net.name)
        for remark in moved:
            _note(remark)
        # An end that carries a via of this path is not looked at: the via is what it leads to.
        tracks = [item for item in created if isinstance(item, Track)]
        ends = []
        if len(runs[0]["points"]) > 1:
            ends.append(("starts", runs[0]["points"][0], tracks[0]))
        if len(runs[-1]["points"]) > 1 and not runs[-1]["via"]:
            ends.append(("ends", runs[-1]["points"][-1], tracks[-1]))
        for verb, point, track in ends:
            if not copper.reaches(point, track.layer, net.name, track.width):
                place = f"{format_mm(Vector2.from_xy(*point))} on {layer_name(track.layer)}"
                _note(f"the track {verb} at {place}, where there is no other {net.name} copper")
        for corner in sharp:
            _note(f"the track turns back on itself at {format_mm(Vector2.from_xy(*corner))}")


def _routed(board) -> dict:
    """The tracks and vias of the board by ID."""
    return {item.id.value: item for item in (*board.get_tracks(), *board.get_vias())}


def _renetted(was, now: dict) -> list[str]:
    """A remark for the tracks and vias that are on another net than ``was``, pairs of an ID and
    a net name, has for them. KiCad gives the tracks and vias that connect one net, that of the
    pad they lead to, unless pads of two nets are among them; it does so for new copper and for
    copper that a moved pad lands on."""
    moved: dict[tuple[str, str], list[str]] = {}
    for key, name in was:
        item = now.get(key)
        if item is not None and item.net.name != name:
            kind = "via" if isinstance(item, Via) else "track"
            moved.setdefault((name, item.net.name), []).append(f"{kind} {short_id(item)}")
    return [
        f"KiCad moved {', '.join(items)} from {_net_words(old)} to {_net_words(new)}: tracks "
        "and vias that connect take the net of the pad they lead to"
        for (old, new), items in moved.items()
    ]


def _settled(board, copper: _Copper, created, net_name: str) -> tuple[list, list[str]]:
    """The new tracks and vias as the board holds them, and the remarks of `_renetted` for them
    and for the copper that was there. ``create_items`` answers before KiCad has settled the
    nets, so the nets it returns can be stale."""
    now = _routed(board)
    was = [(item.id.value, net_name) for item in created]
    was += [(item.id.value, item.net.name) for item in (*copper.tracks, *copper.vias)]
    return [now.get(item.id.value, item) for item in created], _renetted(was, now)


def _net_words(name: str) -> str:
    return f"net {name}" if name else "no net"


def _note_foreign(copper: _Copper, created, net_name: str) -> None:
    """Say which copper of other nets the new tracks and vias lie on."""
    seen = set()
    for item in created:
        if isinstance(item, Via):
            kind, place = "via", _xy(item.position)
            others = copper.foreign(place, place, item.diameter, None, net_name)
        else:
            kind = "track"
            ends = _xy(item.start), _xy(item.end)
            others = copper.foreign(*ends, item.width, item.layer, net_name)
        for other in others:
            if other not in seen:
                seen.add(other)
                _note(f"{kind} {short_id(item)} overlaps {other}")


def _cmd_via(args):
    if (args.array is None) != (args.pitch is None):
        raise CliError("--array and --pitch go together")
    offsets = [(0, 0)]
    if args.array is not None:
        shape = _ARRAY.fullmatch(args.array)
        pitch = re.fullmatch(rf"({_NUMBER})(?:,({_NUMBER}))?", args.pitch)
        if not shape or not pitch:
            raise CliError("expected --array COLUMNSxROWS and --pitch MM or --pitch DX,DY")
        columns, rows = int(shape[1]), int(shape[2])
        step_x, step_y = float(pitch[1]), float(pitch[2] or pitch[1])
        offsets = [
            (_nm((column - (columns - 1) / 2) * step_x), _nm((row - (rows - 1) / 2) * step_y))
            for row in range(rows)
            for column in range(columns)
        ]
    with _board(args) as board:
        positions = _Positions(board)
        resolved = positions.resolve(args.points)
        copper = _Copper(board, positions.footprints())
        net = _pick_net(board, copper, args.net, resolved)
        netclass = _netclass(board, net)
        vias = [
            _new_via((position.x + dx, position.y + dy), net, netclass, args.size, args.drill)
            for position, _pad in resolved
            for dx, dy in offsets
        ]
        created = board.create_items(vias)
        board.save()
        created, moved = _settled(board, copper, created, net.name)
        _emit_copper(args, created)
        _note_foreign(copper, created, net.name)
        for remark in moved:
            _note(remark)


def _area_arguments(args, *, with_net: bool) -> tuple[str | None, list[str]]:
    """Split the positional arguments of 'zone' and 'keepout' into a net name and corners."""
    names = [token for token in args.targets if not _XY.fullmatch(token)]
    corners = [token for token in args.targets if _XY.fullmatch(token)]
    if len(names) > with_net:
        raise CliError(f"bad position {names[with_net]!r}: expected X,Y in millimetres")
    if args.edit is None and (len(names) < with_net or len(corners) < 2):
        what = "the net and the corners" if with_net else "the corners"
        raise CliError(f"give {what} of the area, or --edit ID to change an existing one")
    return (names[0] if names else None), corners


def _finish_area(args, board, zone, *, new: bool) -> None:
    stored = board.create_items(zone)[0] if new else board.update_items(zone)[0]
    refill_zones(board)
    board.save()
    stored = board.get_items_by_id(stored.id)[0]
    if stored.is_rule_area():
        data = {"keepouts": [_keepout_data(stored)]}
        emit(args, data, lambda data: [f"keepout {_keepout_row(k)}" for k in data["keepouts"]])
    else:
        data = {"zones": [_zone_data(stored)]}
        emit(args, data, lambda data: [f"zone {_zone_row(zone)}" for zone in data["zones"]])


def _cmd_zone(args):
    net, corners = _area_arguments(args, with_net=True)
    new = args.edit is None
    with _board(args) as board:
        zone = Zone() if new else _find_zone(board, args.edit, keepout=False)
        if net is not None:
            zone.net = _find_net(board, net)
        if args.layer is not None or new:
            zone.layers = [_copper_layer(name) for name in (args.layer or "F.Cu").split(",")]
        if corners:
            zone.outline.outline = _polyline(_corners(corners))
        if args.name is not None:
            zone.name = args.name
        if args.priority is not None:
            zone.priority = args.priority
        if args.clearance is not None:
            zone.clearance = _nm(args.clearance)
        if args.min_width is not None:
            zone.min_thickness = _nm(args.min_width)
        connection = zone.connection
        spokes = connection.thermal_spokes
        if args.solid:
            connection.zone_connection = ZoneConnectionStyle.ZCS_FULL
        elif new or args.thermal:
            # Left unset, pads would be flooded solid, which makes them hard to solder by hand.
            connection.zone_connection = ZoneConnectionStyle.ZCS_THERMAL
            spokes.width = spokes.width or _nm(_THERMAL_SPOKE_MM)
            spokes.gap = spokes.gap or _nm(_THERMAL_GAP_MM)
        if args.thermal_gap is not None:
            spokes.gap = _nm(args.thermal_gap)
        if args.spoke_width is not None:
            spokes.width = _nm(args.spoke_width)
        if args.islands == "keep":
            zone.island_mode = IslandRemovalMode.IRM_NEVER
        elif args.islands == "remove":
            zone.island_mode = IslandRemovalMode.IRM_ALWAYS
        elif args.islands is not None:
            try:
                area = float(args.islands)
            except ValueError:
                raise CliError("--islands takes keep, remove or an area in mm2") from None
            zone.island_mode = IslandRemovalMode.IRM_AREA
            zone.min_island_area = round(area * 1e12)
        _finish_area(args, board, zone, new=new)


def _cmd_keepout(args):
    _net, corners = _area_arguments(args, with_net=False)
    new = args.edit is None
    with _board(args) as board:
        if new:
            zone = Zone()
            zone.type = ZoneType.ZT_RULE_AREA
        else:
            zone = _find_zone(board, args.edit, keepout=True)
        if args.layer is not None:
            zone.layers = [_copper_layer(name) for name in args.layer.split(",")]
        elif new:
            zone.layers = _copper_layers(board)
        if corners:
            zone.outline.outline = _polyline(_corners(corners))
        if args.name is not None:
            zone.name = args.name
        if args.no is not None or new:
            banned = (args.no or "tracks,vias,copper").split(",")
            for kind in banned:
                if kind not in _KEEPOUT_KINDS:
                    raise CliError(f"--no takes {', '.join(_KEEPOUT_KINDS)}; got {kind!r}")
            for kind in _KEEPOUT_KINDS:
                setattr(zone.rule_area_settings, f"keepout_{kind}", kind in banned)
        _finish_area(args, board, zone, new=new)


def _cmd_at(args):
    with _board(args) as board:
        footprints = board.get_footprints()
        place = _xy(_point(args.at))
        probe, reach = Shape((place,)), _nm(args.radius)
        pieces = _pieces(board, footprints)
        areas = board.get_zones()
        layers = []
        for layer in _copper_layers(board):
            near = []
            for piece in pieces:
                shapes = piece.shapes.get(layer)
                if shapes:
                    distance = min(gap(probe, shape)[0] for shape in shapes)
                    if distance <= reach:
                        near.append((distance, piece))
            near.sort(key=lambda entry: entry[0])
            found = {
                "layer": layer_name(layer),
                "copper": [
                    {**_piece_data(piece), "distance": mm(distance)} for distance, piece in near
                ],
                "unfilled_zones": [],
                "keepouts": [],
            }
            for zone in areas:
                outline = [_xy(n.point) for n in zone.outline.outline.nodes if n.has_point]
                if layer not in zone.layers or not point_in_polygon(place, outline):
                    continue
                if zone.is_rule_area():
                    found["keepouts"].append(_keepout_data(zone))
                elif all(piece.key != zone.id.value for _distance, piece in near):
                    net = zone.net.name if zone.net is not None else ""
                    found["unfilled_zones"].append({"id": zone.id.value, "net": net or None})
            if found["copper"] or found["unfilled_zones"] or found["keepouts"]:
                layers.append(found)
        inside = []
        for footprint in footprints:
            box = _courtyard(footprint)
            if box is not None:
                left, top, right, bottom = box
                dx = max(left - place[0], place[0] - right, 0)
                dy = max(top - place[1], place[1] - bottom, 0)
                if math.hypot(dx, dy) <= reach:
                    inside.append(_reference(footprint))
        emit(args, {"layers": layers, "courtyards": sorted(inside, key=natural)}, _at_lines)


def _at_lines(data: dict) -> list[str]:
    lines = []
    for layer in data["layers"]:
        lines.append(f"{layer['layer']}:")
        lines += [f"  {_piece_row(piece)}  {num(piece['distance'])}" for piece in layer["copper"]]
        lines += [
            f"  zone {zone['id'][:8]} ({zone['net'] or 'no net'})  outline only, no fill"
            for zone in layer["unfilled_zones"]
        ]
        lines += [f"  keepout {_keepout_row(keepout)}" for keepout in layer["keepouts"]]
    if data["courtyards"]:
        lines.append("courtyards: " + " ".join(data["courtyards"]))
    if not data["layers"]:
        lines.append("no copper there")
    return lines


def _find_pieces(pieces: list[_Piece], footprints, target: str) -> list[_Piece]:
    references = {_reference(footprint) for footprint in footprints}
    if target in references:
        matches = [piece for piece in pieces if piece.reference == target]
    elif target.partition(".")[0] in references:
        matches = [piece for piece in pieces if piece.reference and piece.key == target]
    else:
        matches = [p for p in pieces if not p.reference and p.key.startswith(target)]
        if len(matches) > 1:
            raise CliError(f"{target} matches several objects")
    if not matches:
        raise CliError(
            f"{target} is not REF.PAD, a reference, or the ID of a track, via or filled zone"
        )
    return matches


def _cmd_gap(args):
    with _board(args) as board:
        footprints = board.get_footprints()
        pieces = _pieces(board, footprints)
        first = _find_pieces(pieces, footprints, args.first)
        second = _find_pieces(pieces, footprints, args.second)

        def measured(layer, found) -> dict:
            distance, p, q, a, b = found
            return {
                "layer": None if layer is None else layer_name(layer),
                "distance": mm(distance),
                "touching": distance == 0,
                "first": {**_piece_data(a), "point": {"x": mm(p[0]), "y": mm(p[1])}},
                "second": {**_piece_data(b), "point": {"x": mm(q[0]), "y": mm(q[1])}},
            }

        def layers_of(group) -> list:
            return [x for x in _copper_layers(board) if any(p.shapes.get(x) for p in group)]

        layers_a, layers_b = layers_of(first), layers_of(second)
        for target, layers in ((args.first, layers_a), (args.second, layers_b)):
            if not layers:
                raise CliError(f"{target} has no copper; fill the zones with 'cuprum pcb fill'")
        shared = [layer for layer in layers_a if layer in layers_b]
        data = {
            "first": {"target": args.first, "layers": [layer_name(x) for x in layers_a]},
            "second": {"target": args.second, "layers": [layer_name(x) for x in layers_b]},
            "gaps": [measured(x, _closest(first, second, x, x)) for x in shared],
            "seen_from_above": None,
        }
        if not shared:
            crossings = [_closest(first, second, a, b) for a in layers_a for b in layers_b]
            data["seen_from_above"] = measured(None, min(crossings, key=lambda found: found[0]))
        emit(args, data, _gap_lines)


def _gap_lines(data: dict) -> list[str]:
    def row(title: str, found: dict) -> str:
        a, b = _piece_row(found["first"]), _piece_row(found["second"])
        p, q = xy(found["first"]["point"]), xy(found["second"]["point"])
        if found["touching"]:
            return f"{title}  0  {a} and {b} touch or overlap at {p}"
        return f"{title}  {num(found['distance'])}  {a} @{p}  {b} @{q}"

    lines = [row(found["layer"], found) for found in data["gaps"]]
    if data["seen_from_above"] is not None:
        first, second = data["first"], data["second"]
        lines.append(
            f"no copper layer in common: {first['target']} is on {' '.join(first['layers'])}, "
            f"{second['target']} on {' '.join(second['layers'])}"
        )
        lines.append(row("seen from above", data["seen_from_above"]))
    return lines


def _cmd_fill(args):
    with _board(args) as board:
        zones = board.get_zones()
        if zones:
            refill_zones(board)
            board.save()
    emit(
        args,
        {"refilled_zones": len(zones)},
        lambda data: [f"refilled {data['refilled_zones']} zones"],
    )


def _cmd_text(args):
    with _board(args) as board:
        text = BoardText()
        text.value = args.text
        text.position = _point(args.at)
        text.layer = _layer(args.layer)
        text.attributes.size = Vector2.from_xy(_nm(args.size), _nm(args.size))
        text.attributes.stroke_width = _nm(args.size * 0.15)
        text.attributes.angle = args.rot
        created = board.create_items(text)[0]
        if args.rot and set_text_angles(board, {created.id.value: args.rot}):
            created = board.get_items_by_id(created.id)[0]
        board.save()
        emit(args, {"texts": [_text_data(created)]}, _track_and_text_lines)


def _track_and_text_lines(data: dict) -> list[str]:
    tracks = [f"track {_track_row(track)}" for track in data.get("tracks", [])]
    return tracks + [f"text {_text_row(text)}" for text in data["texts"]]


def _cmd_edit(args):
    for_tracks = args.width is not None
    for_text = any(v is not None for v in (args.at, args.text, args.size, args.rot))
    if not (for_tracks or for_text):
        raise CliError(
            "nothing to do: give --width for tracks, or --at/--text/--size/--rot for text"
        )
    with _board(args) as board:
        tracks, texts = board.get_tracks(), board.get_text()
        chosen = []
        for target in args.targets:
            matches = [item for item in [*tracks, *texts] if item.id.value.startswith(target)]
            if len(matches) != 1:
                problem = (
                    "matches several objects" if matches else "is not the ID of a track or text"
                )
                raise CliError(f"{target} {problem}")
            chosen.append(matches[0])
        if args.net is not None:
            net_name = _find_net(board, args.net).name
            chosen += [track for track in tracks if track.net.name == net_name]
        if not chosen:
            raise CliError("nothing selected: give IDs, or --net for all tracks of a net")

        # An update through the API loses the angle of a text; see set_text_angles.
        angles = text_angles(board, [item.id.value for item in chosen]) if for_text else {}
        for item in chosen:
            is_text = any(item is text for text in texts)
            if (is_text and for_tracks) or (not is_text and for_text):
                kind, option = ("text", "--width") if is_text else ("a track", "the text options")
                raise CliError(f"{short_id(item)} is {kind}; {option} do not apply to it")
            if not is_text:
                item.width = _nm(args.width)
                continue
            if args.at is not None:
                item.position = _point(args.at)
            if args.text is not None:
                item.value = args.text
            if args.size is not None:
                item.attributes.size = Vector2.from_xy(_nm(args.size), _nm(args.size))
                item.attributes.stroke_width = _nm(args.size * 0.15)
            if args.rot is not None:
                angles[item.id.value] = args.rot
            # What the API holds for the angle can be stale; the saved one is the real one.
            item.attributes.angle = angles.get(item.id.value, 0.0)
        updated = board.update_items(chosen)
        if angles and set_text_angles(board, angles):
            updated = board.get_items_by_id([item.id for item in updated])
        board.save()
        data = {
            "tracks": [_track_data(x) for x in updated if not isinstance(x, BoardText)],
            "texts": [_text_data(x) for x in updated if isinstance(x, BoardText)],
        }
        emit(args, data, _track_and_text_lines)


def _cmd_field(args):
    if all(v is None for v in (args.rot, args.size, args.visible)) and not any(
        _XY.fullmatch(target) for target in args.targets
    ):
        raise CliError("nothing to do: give a position, --rot, --size, --hide or --show")
    places = [target for target in args.targets if _XY.fullmatch(target)]
    references = [target for target in args.targets if not _XY.fullmatch(target)]
    names = ["reference", "value"] if args.name == "both" else [args.name]
    with _board(args) as board:
        footprints = _select_footprints(board, references, args.all, args.exclude)
        if places and (len(places) > 1 or len(footprints) > 1 or len(names) > 1):
            raise CliError("a position applies to one text of one footprint")
        angles = {}
        for footprint in footprints:
            for name in names:
                field = getattr(footprint, f"{name}_field")
                if places:
                    field.text.position = _point(places[0])
                if args.rot is not None:
                    field.text.attributes.angle = angles[field.text.id.value] = args.rot
                if args.size is not None:
                    field.text.attributes.size = Vector2.from_xy(_nm(args.size), _nm(args.size))
                    field.text.attributes.stroke_width = _nm(args.size * 0.15)
                if args.visible is not None:
                    field.visible = args.visible
        footprints = update_footprints(board, footprints, angles=angles)
        board.save()
        fields = [
            {"reference": _reference(f), **_field_data(getattr(f, f"{name}_field"), name)}
            for f in footprints
            for name in names
        ]
        emit(args, {"fields": fields}, _fields_lines)


def _fields_lines(data: dict) -> list[str]:
    """One line for each text, or just how many footprints there were when there are several."""
    fields = data["fields"]
    names = list(dict.fromkeys(field["name"] for field in fields))
    if len(fields) > len(names):
        count = len(fields) // len(names)
        return [f"changed the {' and '.join(names)} text of {count} footprints"]
    return [f"{field['reference']} {_field_row(field)}" for field in fields]


def _cmd_rm(args):
    with _board(args) as board:
        footprints = board.get_footprints()

        @cache
        def others():
            return (
                [("track", item) for item in board.get_tracks()]
                + [("via", item) for item in board.get_vias()]
                + [("zone", item) for item in board.get_zones()]
                + [("shape", item) for item in board.get_shapes()]
                + [("text", item) for item in board.get_text()]
            )

        doomed, removed = [], []
        for target in args.targets:
            by_reference = [f for f in footprints if _reference(f) == target]
            if by_reference:
                doomed += by_reference
                removed.append({"kind": "footprint", "reference": target, "id": None})
                continue
            matches = [(kind, item) for kind, item in others() if item.id.value.startswith(target)]
            if len(matches) != 1:
                problem = (
                    "matches several objects" if matches else "is neither a reference nor an ID"
                )
                raise CliError(
                    f"{target} {problem}; nothing was removed. "
                    "'cuprum pcb show' lists the current IDs"
                )
            kind, item = matches[0]
            doomed.append(item)
            removed.append({"kind": kind, "reference": None, "id": item.id.value})
        board.remove_items(doomed)
        board.save()
        emit(
            args,
            {"removed": removed},
            lambda data: [
                f"removed {row['kind']} {row['reference'] or row['id'][:8]}"
                for row in data["removed"]
            ],
        )


def _cmd_drc(args):
    with _board(args) as board:
        if board.get_zones():
            # Zone copper is part of what DRC checks, and it goes stale whenever something moves.
            refill_zones(board)
            board.save()
        try:
            violations = run_drc(board, schematic_parity=not args.no_parity)
        except RuntimeError as error:
            # A PCB editor running on its own cannot reach the schematic.
            if args.no_parity or "parity" not in str(error):
                raise
            _note("the schematic could not be read, so parity was not checked")
            violations = run_drc(board, schematic_parity=False)
    data = {"violations": [violation_data(violation, _at) for violation in violations]}
    emit(args, data, lambda data: violation_lines(data) or ["DRC reported no violations"])


def _cmd_rules(args):
    # The net class and rule commands end by showing the rules, with arguments of their own.
    wanted = {key: getattr(args, key, None) for key in _BOARD_CONSTRAINTS}
    wanted = {key: value for key, value in wanted.items() if value is not None}
    with _board(args) as board:
        rules = board.get_design_rules().rules
        if wanted:
            for key, value in wanted.items():
                group, attribute, _ = _BOARD_CONSTRAINTS[key]
                setattr(getattr(rules, group), attribute, _nm(value))
            rules = board.set_design_rules(rules).rules
            board.save()
        project = _project(board)
        assignments = project.get_net_class_assignments()
        data = {
            "copper_layer_count": board.get_copper_layer_count(),
            "constraints": {
                key: mm(getattr(getattr(rules, group), attribute))
                for key, (group, attribute, _) in _BOARD_CONSTRAINTS.items()
            },
            "netclasses": [
                {
                    "name": netclass.name,
                    "clearance": mm(netclass.clearance),
                    "track_width": mm(netclass.track_width),
                    "via_diameter": mm(netclass.via_diameter),
                    "via_drill": mm(netclass.via_drill),
                }
                for netclass in project.get_net_classes()
            ],
            "pattern_assignments": [
                {"pattern": assignment.pattern, "netclass": assignment.netclass}
                for assignment in assignments.pattern_assignments
            ],
            "net_assignments": [
                {"net": assignment.net, "netclasses": list(assignment.netclasses)}
                for assignment in assignments.assignments
            ],
            "rules": [_rule_data(rule) for rule in board.get_custom_design_rules().rules],
        }
    emit(args, data, _rules_lines)


def _rules_lines(data: dict) -> list[str]:
    def sizes(row: dict) -> str:
        return (
            f"clearance {num(row['clearance'])}  track {num(row['track_width'])}  "
            f"via {num(row['via_diameter'])}/{num(row['via_drill'])}"
        )

    # The constraints go by the names of their options, a few to a line.
    constraints = [
        f"{key.replace('_', '-')} {num(value)}" for key, value in data["constraints"].items()
    ]
    lines = [f"copper layers: {data['copper_layer_count']}"]
    lines += [
        ("constraints: " if start == 0 else "  ") + "  ".join(constraints[start : start + 4])
        for start in range(0, len(constraints), 4)
    ]
    lines += [f"netclass {netclass['name']}: {sizes(netclass)}" for netclass in data["netclasses"]]
    lines += [
        f"  nets matching {row['pattern']} -> {row['netclass']}"
        for row in data["pattern_assignments"]
    ]
    lines += [
        f"  net {row['net']} -> {', '.join(row['netclasses'])}" for row in data["net_assignments"]
    ]
    return lines + [f"rule {_rule_row(rule)}" for rule in data["rules"]]


def _rule_data(rule) -> dict:
    """A custom rule. Its layer is a copper layer, ``outer``, ``inner`` or null for all."""
    constraints = []
    for constraint in rule.constraints:
        numeric = constraint.numeric
        constraints.append(
            {
                "type": CustomRuleConstraintType.Name(constraint.type)
                .removeprefix("CRCT_")
                .lower(),
                **{
                    bound: mm(getattr(numeric, bound))
                    if numeric is not None and numeric.HasField(bound)
                    else None
                    for bound in ("min", "opt", "max")
                },
            }
        )
    layer = None
    if rule.single_layer is not None:
        layer = layer_name(rule.single_layer)
    elif rule.layer_mode is not None:
        layer = CustomRuleLayerMode.Name(rule.layer_mode)[5:].lower()
    return {
        "name": rule.name,
        "condition": rule.condition or None,
        "layer": layer,
        "constraints": constraints,
    }


def _rule_row(data: dict) -> str:
    parts = []
    for constraint in data["constraints"]:
        limits = [
            f"{bound} {num(constraint[bound])}"
            for bound in ("min", "opt", "max")
            if constraint[bound] is not None
        ]
        parts.append(" ".join([constraint["type"].replace("_", "-"), *limits]))
    if data["layer"] in _RULE_LAYER_MODES:
        parts.append(f"on {data['layer']} layers")
    elif data["layer"] is not None:
        parts.append(f"on {data['layer']}")
    return f"{data['name']}: {'  '.join(parts)}  when {data['condition'] or 'always'}"


def _cmd_rule(args):
    limits = {kind: getattr(args, kind) for kind in _RULE_CONSTRAINTS}
    limits = {kind: value for kind, value in limits.items() if value is not None}
    with _board(args) as board:
        rules = list(board.get_custom_design_rules().rules)
        names = [rule.name for rule in rules]
        if args.name is None:
            if limits or args.remove:
                raise CliError("name the rule")
        elif args.remove:
            if args.name not in names:
                raise CliError(f"no rule named {args.name}; the rules are {', '.join(names)}")
            rules = [rule for rule in rules if rule.name != args.name]
        else:
            if not limits:
                options = ", ".join("--" + kind.replace("_", "-") for kind in _RULE_CONSTRAINTS)
                raise CliError(f"say what the rule asks for: {options}")
            rule = CustomRule()
            rule.name, rule.condition = args.name, args.condition or ""
            constraints = []
            for kind, value in limits.items():
                constraint = CustomRuleConstraint.from_type(_RULE_CONSTRAINTS[kind])
                minimum = MinOptMax()
                minimum.min = _nm(value)
                constraint.numeric = minimum
                constraints.append(constraint)
            rule.constraints = constraints
            if args.layer in _RULE_LAYER_MODES:
                rule.layer_mode = _RULE_LAYER_MODES[args.layer]
            elif args.layer is not None:
                rule.single_layer = _copper_layer(args.layer)
            # A rule that is given again keeps its place: of two rules that apply to the same
            # objects, the later one wins.
            if args.name in names:
                rules[names.index(args.name)] = rule
            else:
                rules.append(rule)
        if args.name is not None:
            response = board.set_custom_design_rules(rules)
            if response.status == CustomRulesStatus.CRS_INVALID:
                raise CliError(f"KiCad rejected the rule: {response.error_text.strip()}")
            board.save()
            rules = list(board.get_custom_design_rules().rules)
        data = {"rules": [_rule_data(rule) for rule in rules]}
    emit(
        args,
        data,
        lambda data: [f"rule {_rule_row(rule)}" for rule in data["rules"]] or ["no custom rules"],
    )


def _release_nets(project, name: str, patterns: list[str] | None) -> None:
    """Take nets out of a net class: the given patterns and net names, or all of them when
    ``patterns`` is None. Sent as a merge, an assignment to nothing removes the entry."""
    current = project.get_net_class_assignments()
    known = [a.pattern for a in current.pattern_assignments if a.netclass == name]
    known += [a.net for a in current.assignments if name in a.netclasses]
    for pattern in patterns or []:
        if pattern not in known:
            listed = ", ".join(known) or "none"
            raise CliError(f"{pattern} is not assigned to {name}; its assignments are {listed}")
    released = NetClassAssignments()
    for assignment in current.pattern_assignments:
        if assignment.netclass == name and (patterns is None or assignment.pattern in patterns):
            assignment.netclass = ""
            released.pattern_assignments.append(assignment)
    for assignment in current.assignments:
        if name in assignment.netclasses and (patterns is None or assignment.net in patterns):
            assignment.netclasses = [x for x in assignment.netclasses if x != name]
            released.assignments.append(assignment)
    project.set_net_class_assignments(released)


def _cmd_netclass(args):
    with _board(args) as board:
        project = _project(board)
        existing = {netclass.name: netclass for netclass in project.get_net_classes()}
        if args.remove:
            if args.name == "Default" or args.name not in existing:
                raise CliError(
                    f"{args.name} cannot be removed: "
                    + ("every net falls back on it" if args.name in existing else "no such class")
                )
            _release_nets(project, args.name, None)
            del existing[args.name]
            project.set_net_classes(list(existing.values()), merge_mode=MapMergeMode.MMM_REPLACE)
            board.save()
            _cmd_rules(args)
            return
        netclass = existing.get(args.name) or NetClass(
            project_settings_pb2.NetClass(name=args.name)
        )
        if args.clearance is not None:
            netclass.clearance = _nm(args.clearance)
        if args.track_width is not None:
            netclass.track_width = _nm(args.track_width)
        if args.via_size is not None:
            netclass.via_diameter = _nm(args.via_size)
        if args.via_drill is not None:
            netclass.via_drill = _nm(args.via_drill)
        project.set_net_classes([netclass])
        if args.release:
            _release_nets(project, args.name, args.release)
        if args.nets:
            patterns = []
            for pattern in args.nets:
                assignment = NetClassPatternAssignment()
                assignment.pattern, assignment.netclass = pattern, args.name
                patterns.append(assignment)
            project.set_net_class_assignments(NetClassAssignments(pattern_assignments=patterns))
        board.save()
    _cmd_rules(args)


def _cmd_layers(args):
    with _board(args) as board:
        if args.count is not None:
            if args.count < 2 or args.count % 2:
                raise CliError("the number of copper layers must be even and at least 2")
            changed = args.count != board.get_copper_layer_count()
            keep = [x for x in board.get_enabled_layers() if not is_copper_layer(x)]
            board.set_enabled_layers(args.count, keep)
            board.save()
            if changed and reset_stackup(board):
                _note(
                    f"the stackup is back to KiCad's default for {args.count} layers; "
                    "set it again with 'pcb stackup'"
                )
        copper = [layer_name(x) for x in board.get_enabled_layers() if is_copper_layer(x)]
    emit(
        args,
        {"copper_layers": copper},
        lambda data: [f"copper layers: {' '.join(data['copper_layers'])}"],
    )


def _stackup_data(board) -> list[dict]:
    """The layers of the stackup that have a thickness, top to bottom. A dielectric is called
    D1, D2, ... from the top; ``sublayers`` counts what it is laminated from."""
    stackup = board.get_stackup()
    rows = []
    for name, layer in zip(stackup_names(stackup), stackup.layers):
        row = {
            "name": name.replace("dielectric ", "D"),
            "file_name": name,
            "type": None,
            "thickness": mm(layer.thickness),
            "material": None,
            "epsilon_r": None,
            "loss_tangent": None,
            "sublayers": 1,
        }
        if layer.type == BoardStackupLayerType.BSLT_COPPER:
            row["type"] = "copper"
        elif layer.type == BoardStackupLayerType.BSLT_SOLDERMASK:
            row["type"] = "solder mask"
        elif layer.type == BoardStackupLayerType.BSLT_DIELECTRIC:
            core = layer.dielectric.type == BoardStackupDielectricType.BSDT_CORE
            material = layer.dielectric.layers[0]
            row |= {
                "type": "core" if core else "prepreg",
                "material": material.material_name,
                "epsilon_r": number(material.epsilon_r),
                "loss_tangent": number(material.loss_tangent, 6),
                "sublayers": len(layer.dielectric.layers),
            }
        else:
            continue
        rows.append(row)
    return rows


def _cmd_stackup(args):
    with _board(args) as board:
        layers = _stackup_data(board)
        by_name = {row["name"]: row for row in layers}
        changes: dict[str, dict[str, float | str]] = {}
        for key in ("thickness", "type", "epsilon_r", "loss_tangent"):
            for token in getattr(args, key) or []:
                name, _, value = token.partition("=")
                row = by_name.get(name)
                if row is None:
                    raise CliError(f"no layer {name!r}; the stackup has {', '.join(by_name)}")
                if key != "thickness" and row["material"] is None:
                    raise CliError(f"{name} is not a dielectric")
                if row["sublayers"] > 1:
                    raise CliError(
                        f"{name} is laminated from {row['sublayers']} layers, which this command "
                        "cannot tell apart; change it in KiCad's board setup"
                    )
                if key == "type":
                    if value not in ("core", "prepreg"):
                        raise CliError(f"bad type in {token!r}: expected core or prepreg")
                else:
                    try:
                        value = float(value)
                    except ValueError:
                        raise CliError(f"bad value in {token!r}: expected a number") from None
                    if value < 0 or (key == "thickness" and value == 0):
                        raise CliError(f"bad value in {token!r}")
                changes.setdefault(row["file_name"], {})[key] = value
        if changes:
            set_stackup(board, changes)
            layers = _stackup_data(board)
    for row in layers:
        del row["file_name"]
    data = {"layers": layers, "board_thickness": number(sum(x["thickness"] for x in layers))}
    emit(args, data, _stackup_lines)


def _stackup_lines(data: dict) -> list[str]:
    lines = []
    for row in data["layers"]:
        line = f"{row['name']}: {row['type']}  thickness {num(row['thickness'])}"
        if row["material"] is not None:
            line += (
                f"  {row['material']}  epsilon-r {num(row['epsilon_r'])}"
                f"  loss-tangent {row['loss_tangent']:g}"
            )
        lines.append(line)
    return lines + [f"board thickness: {num(data['board_thickness'])}"]


def _cmd_render(args):
    schematic = resolve_schematic(args.file)
    board = schematic.with_suffix(".kicad_pcb")
    if not board.exists():
        raise CliError(f"{board} does not exist; create it with 'cuprum pcb sync'")
    # A custom set of layers or an area is named after it, so it does not replace the usual
    # picture.
    name = f"{schematic.stem}-pcb"
    if args.layers != _DEFAULT_RENDER_LAYERS:
        name += "-" + args.layers.replace(",", "+")
    layers = args.layers.split(",")
    if args.area is None:
        output = render_directory(schematic, args.out) / f"{name}.png"
        output = render_board(board, output, layers=layers, dpi=args.dpi)
        emit(args, {"files": [str(output)], "area": None, "pixels_per_mm": None}, _render_lines)
        return
    corners = [_point(token) for token in args.area]
    xs, ys = sorted(c.x / 1e6 for c in corners), sorted(c.y / 1e6 for c in corners)
    if xs[0] == xs[1] or ys[0] == ys[1]:
        raise CliError("--area takes two opposite corners of a rectangle")
    area = area_data(xs, ys)
    place = area_row(area).replace(" -> ", "-")
    output = render_directory(schematic, args.out) / f"{name}-{place}.png"
    scale = render_board_area(
        board, output, (xs[0], ys[0], xs[1], ys[1]), layers=layers, dpi=args.dpi
    )
    data = {"files": [str(output)], "area": area, "pixels_per_mm": number(scale, 6)}
    emit(args, data, _render_lines)


def _render_lines(data: dict) -> list[str]:
    lines = list(data["files"])
    if data["area"] is not None:
        lines.append(f"shows {area_row(data['area'])} at {data['pixels_per_mm']:g} pixels per mm")
    return lines


# ---------------------------------------------------------------------------
# Arguments


def register(subparsers, common) -> None:
    """Add the ``pcb`` command group to the top-level parser."""
    summary = "board commands: sync from the schematic, place, route, pour, check, render"
    pcb = subparsers.add_parser(
        "pcb",
        help=summary,
        description=summary + ". Coordinates and sizes are in millimetres. A position is X,Y, "
        "or REF.PAD (R1.2) where a pad is meant.",
    )
    sub = pcb.add_subparsers(dest="pcb_command", required=True, metavar="COMMAND")

    def command(name, handler, help_text):
        subparser = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        subparser.set_defaults(handler=handler)
        return subparser

    p = command(
        "sync", _cmd_sync, "update the board from the schematic, creating the board if needed"
    )
    p.add_argument(
        "--delete-extra", action="store_true", help="remove footprints that have no symbol"
    )
    p.add_argument(
        "--by-reference",
        action="store_true",
        help="match footprints to symbols by reference, so that a symbol which was deleted and "
        "placed again keeps its footprint",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="print KiCad's full change report")

    p = command(
        "show", _cmd_show, "list what is on the board; with references, show their pads and sizes"
    )
    p.add_argument("references", nargs="*", metavar="REF")
    p.add_argument("--net", help="show only the pads, tracks, vias and zones of this net")

    p = command("move", _cmd_move, "place a footprint: position, rotation and board side")
    p.add_argument("reference", metavar="REF")
    p.add_argument("at", nargs="?", metavar="X,Y", help="new position of the footprint origin")
    p.add_argument("--rot", type=float, metavar="DEGREES", help="absolute rotation")
    p.add_argument("--side", choices=["front", "back"])
    p.add_argument("--pads", action="store_true", help="also list where the pads are now")

    p = command(
        "add", _cmd_add, "place a footprint that has no symbol, such as a mounting hole or fiducial"
    )
    p.add_argument("lib_id", metavar="LIB:FOOTPRINT")
    p.add_argument("at", metavar="X,Y")
    p.add_argument("--ref", required=True, help="reference, e.g. H1")
    p.add_argument("--rot", type=float, default=0.0, metavar="DEGREES")
    p.add_argument("--side", choices=["front", "back"], default="front")

    p = command("outline", _cmd_outline, "draw the board outline on Edge.Cuts")
    p.add_argument(
        "corners",
        nargs="+",
        metavar="X,Y",
        help="two opposite corners of a rectangle, or the corners of a polygon in order",
    )
    p.add_argument("--radius", type=float, metavar="MM", help="round the corners of a rectangle")
    p.add_argument("--replace", action="store_true", help="replace an existing outline")

    p = command(
        "track",
        _cmd_track,
        "route one connection through the given positions. Every segment comes out horizontal, "
        "vertical or at 45 degrees: a bend is put between two positions that are not in line, "
        "and a square corner is cut at 45 degrees. The segments made are printed",
    )
    p.add_argument(
        "points",
        nargs="+",
        metavar="STEP",
        help="a position: X,Y, REF.PAD, or relative to the one before: @DX,DY, x=X, y=Y. "
        "'via' drops a via at the position before it and goes on on the other side, 'via:LAYER' "
        "on that layer. 'w=MM' changes the track width from there on",
    )
    p.add_argument("--layer", help="layer to start on; defaults to the first pad's side, else F.Cu")
    p.add_argument("--width", type=float, help="defaults to the net class track width")
    p.add_argument("--net", help="defaults to the net of the pads or copper the track touches")
    p.add_argument("--via-size", type=float, help="defaults to the net class via diameter")
    p.add_argument("--via-drill", type=float, help="defaults to the net class via drill")
    p.add_argument(
        "--diagonal",
        choices=DIAGONAL_PLACES,
        default="middle",
        help="where the 45 degree part of a bend goes: in the middle (default), with a straight "
        "part at either end, or right at the start or the end",
    )
    p.add_argument(
        "--chamfer",
        type=float,
        metavar="MM",
        help="how far from a square corner the 45 degree cut starts; defaults to 3 track "
        "widths, never more than half of either side; 0 leaves square corners",
    )
    p.add_argument(
        "--exact",
        action="store_true",
        help="draw straight from position to position, with no bends and no cut corners",
    )

    p = command(
        "via",
        _cmd_via,
        "place through vias, for example to stitch zones together or under a thermal pad",
    )
    p.add_argument("points", nargs="+", metavar="POSITION")
    p.add_argument("--net", help="defaults to the net of the pad or copper under the first via")
    p.add_argument("--size", type=float, help="defaults to the net class via diameter")
    p.add_argument("--drill", type=float, help="defaults to the net class via drill")
    p.add_argument(
        "--array",
        metavar="COLUMNSxROWS",
        help="place a grid of vias centred on each position; needs --pitch",
    )
    p.add_argument("--pitch", metavar="MM|DX,DY", help="spacing of the grid")

    corners_help = "two opposite corners of a rectangle, or the corners of a polygon in order"
    p = command(
        "zone",
        _cmd_zone,
        "pour copper for a net inside an area, or change a zone that exists (--edit); all "
        "zones are refilled afterwards",
    )
    p.add_argument("targets", nargs="*", metavar="NET|X,Y", help="the net, then " + corners_help)
    p.add_argument(
        "--edit",
        metavar="ID",
        help="change this zone instead of making one: whatever is given (net, corners, "
        "options) replaces what the zone has",
    )
    p.add_argument("--layer", help="copper layers, comma-separated (default F.Cu)")
    p.add_argument("--name")
    p.add_argument("--priority", type=int, help="higher-priority zones are filled first")
    p.add_argument("--clearance", type=float, help="clearance of this zone to other nets")
    p.add_argument(
        "--min-width", type=float, metavar="MM", help="narrowest copper the fill keeps (0.25)"
    )
    connection = p.add_mutually_exclusive_group()
    connection.add_argument(
        "--solid",
        action="store_true",
        help="connect pads of the net solidly instead of through thermal reliefs",
    )
    connection.add_argument(
        "--thermal", action="store_true", help="connect pads through thermal reliefs (default)"
    )
    p.add_argument(
        "--thermal-gap", type=float, metavar="MM", help="gap around a relieved pad (0.5)"
    )
    p.add_argument("--spoke-width", type=float, metavar="MM", help="width of the spokes (0.5)")
    p.add_argument(
        "--islands",
        metavar="remove|keep|MM2",
        help="copper that connects to nothing: remove it (default), keep it, or remove only "
        "islands smaller than this area in mm2",
    )

    p = command(
        "keepout",
        _cmd_keepout,
        "mark an area that tracks, vias, zone copper, pads or footprints have to stay out of, "
        "or change one that exists (--edit). Zone fills go around it and DRC reports what "
        "enters it",
    )
    p.add_argument("targets", nargs="*", metavar="X,Y", help=corners_help)
    p.add_argument("--edit", metavar="ID", help="change this keepout instead of making one")
    p.add_argument("--layer", help="copper layers, comma-separated (default: all of them)")
    p.add_argument(
        "--no",
        metavar="KIND,KIND",
        help=f"what to keep out, of {', '.join(_KEEPOUT_KINDS)} (default tracks,vias,copper)",
    )
    p.add_argument("--name")

    p = command(
        "at",
        _cmd_at,
        "list the copper at a position, layer by layer: pads, tracks, vias and zone fills with "
        "their nets and the distance from the position to their edge (0 when it lies on them)",
    )
    p.add_argument("at", metavar="X,Y")
    p.add_argument(
        "--radius", type=float, default=0.0, metavar="MM", help="also list what is this near"
    )

    p = command(
        "gap",
        _cmd_gap,
        "measure the copper-to-copper distance between two objects on every layer they share, "
        "and where they come closest. Round, oval, rectangular and rounded pads, tracks and "
        "vias are measured exactly as DRC does; arcs, zone fills and pads of any other form as "
        "outlines of short straight pieces (within about 0.005 mm). Holes, solder mask and a "
        "pad form that differs from layer to layer are not looked at",
    )
    for name in ("first", "second"):
        p.add_argument(
            name,
            metavar="OBJECT",
            help="REF.PAD, a reference (all its pads), or the ID of a track, via or filled zone",
        )

    command("fill", _cmd_fill, "refill all zones after copper has moved")

    p = command("text", _cmd_text, "place text, by default on the front silkscreen")
    p.add_argument("text", metavar="TEXT")
    p.add_argument("at", metavar="X,Y", help="where the middle of the text goes")
    p.add_argument("--layer", default="F.SilkS", help="default F.SilkS")
    p.add_argument("--size", type=float, default=1.0, help="text height (default 1)")
    p.add_argument("--rot", type=float, default=0.0, metavar="DEGREES")

    p = command(
        "edit",
        _cmd_edit,
        "change the width of existing tracks, or move, reword, resize or turn a text",
    )
    p.add_argument("targets", nargs="*", metavar="ID")
    p.add_argument("--net", help="also select every track of this net")
    p.add_argument("--width", type=float, help="new track width")
    p.add_argument("--at", metavar="X,Y", help="new text position")
    p.add_argument("--text", help="new text")
    p.add_argument("--size", type=float, help="new text height")
    p.add_argument("--rot", type=float, metavar="DEGREES", help="new text rotation")

    p = command(
        "field",
        _cmd_field,
        "move, turn, resize, hide or show the reference or value text of footprints",
    )
    p.add_argument(
        "targets",
        nargs="*",
        metavar="REF|X,Y",
        help="references; with a single one, X,Y puts the middle of its text there",
    )
    p.add_argument("--all", action="store_true", help="every footprint")
    p.add_argument("--except", dest="exclude", nargs="+", default=[], metavar="REF")
    p.add_argument("--name", choices=["reference", "value", "both"], default="reference")
    p.add_argument("--rot", type=float, metavar="DEGREES")
    p.add_argument("--size", type=float, help="text height")
    visibility = p.add_mutually_exclusive_group()
    visibility.add_argument("--hide", dest="visible", action="store_false", default=None)
    visibility.add_argument("--show", dest="visible", action="store_true")

    p = command(
        "rm",
        _cmd_rm,
        "delete tracks, vias, zones, shapes and text by the ID from 'show', "
        "or footprints by reference",
    )
    p.add_argument("targets", nargs="+", metavar="ID_OR_REF")

    p = command(
        "drc", _cmd_drc, "refill zones and run DRC; unrouted connections are UNCONNECTED_ITEMS"
    )
    p.add_argument(
        "--no-parity", action="store_true", help="skip the comparison with the schematic"
    )

    p = command(
        "rules",
        _cmd_rules,
        "show the layer count, the board-wide constraints, the net classes and the custom "
        "rules, or set constraints. The constraints are the limits of the manufacturing "
        "process: DRC reports whatever falls below them, whatever the net class. A custom "
        "rule takes the place of a constraint for the objects it picks out, so it can loosen "
        "one as well. They are kept in the project file, the solder mask ones in the board",
    )
    for key, (_, _, meaning) in _BOARD_CONSTRAINTS.items():
        p.add_argument("--" + key.replace("_", "-"), type=float, metavar="MM", help=meaning)

    p = command(
        "rule",
        _cmd_rule,
        "add, replace, remove or (with no arguments) list custom design rules: a requirement "
        "for the objects that a condition in KiCad's rule language picks out, where A and B are "
        "the two objects being compared. Of two rules that apply to the same objects the later "
        "one wins, so an exception goes after the rule it relaxes. DRC and zone fills follow "
        "the rules; they are kept in the project's .kicad_dru file",
    )
    p.add_argument("name", nargs="?", metavar="NAME")
    p.add_argument(
        "condition",
        nargs="?",
        metavar="CONDITION",
        help="for example \"A.NetClass == 'HV' && B.NetClass != 'HV'\"; leave it out for a rule "
        "that always applies",
    )
    p.add_argument("--clearance", type=float, metavar="MM", help="minimum copper clearance")
    p.add_argument("--hole-clearance", type=float, metavar="MM")
    p.add_argument("--hole-to-hole", type=float, metavar="MM")
    p.add_argument("--edge-clearance", type=float, metavar="MM")
    p.add_argument("--track-width", type=float, metavar="MM", help="minimum track width")
    p.add_argument("--annular-width", type=float, metavar="MM", help="minimum annular ring")
    p.add_argument(
        "--silk-clearance",
        type=float,
        metavar="MM",
        help="silkscreen to what the condition picks out; \"B.Type == 'Pad'\" keeps it off "
        "pads, which the board-wide constraint does not",
    )
    p.add_argument("--layer", help="apply on this copper layer only, or on 'outer' or 'inner'")
    p.add_argument("--remove", action="store_true", help="delete the rule")

    p = command(
        "netclass",
        _cmd_netclass,
        "create, change or remove a net class ('Default' is the one every net starts in)",
    )
    p.add_argument("name", metavar="NAME")
    p.add_argument("--clearance", type=float)
    p.add_argument("--track-width", type=float)
    p.add_argument("--via-size", type=float)
    p.add_argument("--via-drill", type=float)
    p.add_argument("--nets", nargs="+", metavar="PATTERN", help="net names or wildcards to assign")
    p.add_argument(
        "--release",
        nargs="+",
        metavar="PATTERN",
        help="take these assignments away again, so the nets fall back to Default",
    )
    p.add_argument(
        "--remove", action="store_true", help="delete the class and all assignments to it"
    )

    p = command("layers", _cmd_layers, "show the copper layers, or set how many there are")
    p.add_argument("count", nargs="?", type=int)

    p = command(
        "stackup",
        _cmd_stackup,
        "show what the board is built from, top to bottom, or change it. The board thickness "
        "is the sum of the layers. Changing the number of copper layers resets the stackup",
    )
    p.add_argument(
        "thickness",
        nargs="*",
        metavar="LAYER=MM",
        help="a new thickness for a layer, by the name this command prints: F.Cu=0.035 D1=0.2",
    )
    p.add_argument("--type", nargs="+", metavar="LAYER=core|prepreg", help="of a dielectric")
    p.add_argument(
        "--epsilon-r", nargs="+", metavar="LAYER=VALUE", help="relative permittivity (D1=4.4)"
    )
    p.add_argument("--loss-tangent", nargs="+", metavar="LAYER=VALUE")

    p = command("render", _cmd_render, "plot the board to a PNG and print the file path")
    p.add_argument(
        "--layers",
        default=_DEFAULT_RENDER_LAYERS,
        help="comma-separated layer names, bottom-most first",
    )
    p.add_argument(
        "--area",
        nargs=2,
        metavar="X,Y",
        help="plot only the rectangle between these two corners, enlarged: its longer side "
        "comes out 8 inches long at the given dpi",
    )
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", help="output directory; defaults to a directory under the temp dir")
