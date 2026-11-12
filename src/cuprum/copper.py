"""Copper as plain shapes and the distances between them: what `pcb at` and `pcb gap` report and
what the notes of `pcb track` go by.

A shape is a point, a segment or a polygon, grown by a radius. A via is a point with its radius,
a track a segment with half its width, a rounded pad a smaller rectangle with the corner radius;
the distance between two such shapes is exact. Everything is in nanometres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from kipy.proto.board.board_types_pb2 import PadStackShape

from cuprum.routing import point_in_polygon

Point = tuple[float, float]

_CHUNK = 16  # edges; consecutive edges of an outline lie close together
_ARC_STEPS = 16


@dataclass(frozen=True)
class Shape:
    points: tuple[Point, ...]
    """One point, the two ends of a segment, or the corners of a polygon."""
    radius: float = 0.0
    holes: tuple[tuple[Point, ...], ...] = ()

    def contains(self, point: Point) -> bool:
        """Whether the point lies inside the polygon, before it is grown by the radius."""
        if len(self.points) < 3 or not point_in_polygon(point, list(self.points)):
            return False
        return not any(point_in_polygon(point, list(hole)) for hole in self.holes)

    def edges(self) -> list[tuple[Point, Point]]:
        edges = []
        for ring in (self.points, *self.holes):
            if len(ring) < 3:
                edges.append((ring[0], ring[-1]))
            else:
                edges += zip(ring, ring[1:] + ring[:1], strict=True)
        return edges


def gap(a: Shape, b: Shape) -> tuple[float, Point, Point]:
    """The distance between the edges of two shapes and the points where they come closest.

    Shapes that touch or overlap give zero and one point that lies in both.
    """
    for outer, inner in ((a, b), (b, a)):
        if outer.contains(inner.points[0]):
            return 0.0, inner.points[0], inner.points[0]
    best = (math.inf, a.points[0], b.points[0])
    chunks_b = _chunks(b.edges())
    for box_a, edges_a in _chunks(a.edges()):
        for box_b, edges_b in chunks_b:
            if _box_gap(box_a, box_b) >= best[0]:
                continue
            for edge_a in edges_a:
                for edge_b in edges_b:
                    found = _segment_gap(*edge_a, *edge_b)
                    if found[0] < best[0]:
                        best = found
    distance, p, q = best
    reach = a.radius + b.radius
    if distance <= reach:
        share = a.radius / reach if reach else 0.0
        meeting = (p[0] + (q[0] - p[0]) * share, p[1] + (q[1] - p[1]) * share)
        return 0.0, meeting, meeting
    ux, uy = (q[0] - p[0]) / distance, (q[1] - p[1]) / distance
    return (
        distance - reach,
        (p[0] + ux * a.radius, p[1] + uy * a.radius),
        (q[0] - ux * b.radius, q[1] - uy * b.radius),
    )


def _chunks(edges):
    chunks = []
    for start in range(0, len(edges), _CHUNK):
        part = edges[start : start + _CHUNK]
        xs = [point[0] for edge in part for point in edge]
        ys = [point[1] for edge in part for point in edge]
        chunks.append(((min(xs), min(ys), max(xs), max(ys)), part))
    return chunks


def _box_gap(a, b) -> float:
    return math.hypot(max(a[0] - b[2], b[0] - a[2], 0), max(a[1] - b[3], b[1] - a[3], 0))


def _nearest(point: Point, start: Point, end: Point) -> Point:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 and dy == 0:
        return start
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return start[0] + t * dx, start[1] + t * dy


def _segment_gap(a1: Point, a2: Point, b1: Point, b2: Point) -> tuple[float, Point, Point]:
    def side(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2, d3, d4 = side(b1, b2, a1), side(b1, b2, a2), side(a1, a2, b1), side(a1, a2, b2)
    if d1 * d2 < 0 and d3 * d4 < 0:
        t = d1 / (d1 - d2)
        crossing = (a1[0] + t * (a2[0] - a1[0]), a1[1] + t * (a2[1] - a1[1]))
        return 0.0, crossing, crossing
    pairs = [(p, _nearest(p, b1, b2)) for p in (a1, a2)]
    pairs += [(_nearest(q, a1, a2), q) for q in (b1, b2)]
    return min((math.dist(p, q), p, q) for p, q in pairs)


# ---------------------------------------------------------------------------
# Board objects as shapes


def pad_shape(pad) -> Shape | None:
    """The copper of a pad, or None when the pad has none or has a form that is not a circle,
    an oval, a rectangle or a rounded rectangle; `polygon_shapes` covers those."""
    layers = pad.padstack.copper_layers
    if not layers:
        return None
    copper = layers[0]
    half_x, half_y = copper.size.x / 2, copper.size.y / 2
    if copper.shape in (PadStackShape.PSS_CIRCLE, PadStackShape.PSS_OVAL):
        radius = min(half_x, half_y)
    elif copper.shape == PadStackShape.PSS_ROUNDRECT:
        radius = min(copper.corner_rounding_ratio * 2, 1.0) * min(half_x, half_y)
    elif copper.shape == PadStackShape.PSS_RECTANGLE:
        radius = 0.0
    else:
        return None
    x, y = half_x - radius, half_y - radius
    corners = [(-x, -y), (x, -y), (x, y), (-x, y)]
    if x == 0 or y == 0:
        corners = list(dict.fromkeys([corners[0], corners[2]]))
    angle = math.radians(pad.padstack.angle.degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    points = tuple(
        (
            pad.position.x + (px + copper.offset.x) * cos + (py + copper.offset.y) * sin,
            pad.position.y - (px + copper.offset.x) * sin + (py + copper.offset.y) * cos,
        )
        for px, py in corners
    )
    return Shape(points, radius)


def polygon_shapes(polygons) -> list[Shape]:
    """kicad-python polygons (zone fills, pads turned into polygons by KiCad) as shapes."""

    def ring(line) -> tuple[Point, ...]:
        return tuple((node.point.x, node.point.y) for node in line.nodes if node.has_point)

    shapes = []
    for polygon in polygons:
        outline = ring(polygon.outline)
        if outline:
            shapes.append(Shape(outline, 0.0, tuple(ring(hole) for hole in polygon.holes)))
    return shapes


def track_shapes(track) -> list[Shape]:
    """A track segment, or an arc track as a run of short segments."""
    start, end = (track.start.x, track.start.y), (track.end.x, track.end.y)
    middle = getattr(track, "mid", None)
    points = _arc(start, (middle.x, middle.y), end) if middle is not None else [start, end]
    return [Shape(pair, track.width / 2) for pair in pairwise(points)]


def via_shape(via) -> Shape:
    return Shape(((via.position.x, via.position.y),), via.diameter / 2)


def _arc(start: Point, middle: Point, end: Point) -> list[Point]:
    (x1, y1), (x2, y2), (x3, y3) = start, middle, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if d == 0:
        return [start, end]
    s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    radius = math.hypot(x1 - cx, y1 - cy)
    a1, a2, a3 = (math.atan2(y - cy, x - cx) for x, y in (start, middle, end))
    sweep, to_middle = (a3 - a1) % math.tau, (a2 - a1) % math.tau
    if to_middle > sweep:
        sweep -= math.tau
    angles = [a1 + sweep * step / _ARC_STEPS for step in range(_ARC_STEPS + 1)]
    return [(cx + radius * math.cos(angle), cy + radius * math.sin(angle)) for angle in angles]
