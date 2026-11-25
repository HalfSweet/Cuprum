"""Geometry for hand routing: turning a list of waypoints into track segments that only run
horizontally, vertically or at 45 degrees and never turn a square corner. And for an outline:
rounding the corners of a polygon.

Everything here works on plain ``(x, y)`` tuples in nanometres.
"""

from __future__ import annotations

import math

Point = tuple[int, int]

DIAGONAL_PLACES = ("middle", "start", "end")
_SLACK = 1.0  # nm; what rounding points to whole nanometres can make two lengths differ by


def bend(start: Point, end: Point, diagonal: str = "middle") -> list[Point]:
    """The points from ``start`` to ``end`` using only horizontal, vertical and 45 degree runs.

    Two points that are already aligned give a single run. Otherwise the offset is split into a
    diagonal run and a straight one along the longer axis; ``diagonal`` says where the diagonal
    goes: in the ``middle`` (straight, diagonal, straight), at the ``start`` or at the ``end``.
    """
    (x1, y1), (x2, y2) = start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 or dy == 0 or abs(dx) == abs(dy):
        return [start, end]
    step_x, step_y = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    slant = min(abs(dx), abs(dy))
    straight = abs(abs(dx) - abs(dy))
    along_x = abs(dx) > abs(dy)

    def run(point: Point, length: int) -> Point:
        if along_x:
            return point[0] + step_x * length, point[1]
        return point[0], point[1] + step_y * length

    def slope(point: Point) -> Point:
        return point[0] + step_x * slant, point[1] + step_y * slant

    if diagonal == "start":
        return [start, slope(start), end]
    if diagonal == "end":
        return [start, run(start, straight), end]
    first = run(start, straight // 2)
    return [start, first, slope(first), end]


def chamfer(
    points: list[Point], widths: list[int], size: int | None = None
) -> tuple[list[Point], list[int]]:
    """Cut every square corner inside the polyline with a 45 degree segment.

    ``widths[i]`` is the width of the run from ``points[i]`` to ``points[i + 1]``. ``size`` is
    how far from the corner the cut starts on each side, by default three times the wider of the
    two runs; it is shortened to half of either run when they are short. The two end points are
    never touched. Returns the new points and widths.
    """
    if len(points) < 3:
        return list(points), list(widths)
    new_points, new_widths = [points[0]], []
    for index in range(1, len(points) - 1):
        before, corner, after = points[index - 1], points[index], points[index + 1]
        width_in, width_out = widths[index - 1], widths[index]
        ax, ay = before[0] - corner[0], before[1] - corner[1]
        bx, by = after[0] - corner[0], after[1] - corner[1]
        length_a, length_b = math.hypot(ax, ay), math.hypot(bx, by)
        square = length_a > 0 and length_b > 0 and ax * bx + ay * by == 0
        wanted = 3 * max(width_in, width_out) if size is None else size
        cut = min(wanted, int(length_a // 2), int(length_b // 2))
        if not square or cut <= 0:
            new_points.append(corner)
            new_widths.append(width_in)
            continue
        new_points.append(
            (corner[0] + round(ax / length_a * cut), corner[1] + round(ay / length_a * cut))
        )
        new_widths.append(width_in)
        new_points.append(
            (corner[0] + round(bx / length_b * cut), corner[1] + round(by / length_b * cut))
        )
        new_widths.append(min(width_in, width_out))
    new_points.append(points[-1])
    new_widths.append(widths[-1])
    return new_points, new_widths


class CornerError(ValueError):
    """A corner of an outline that cannot be rounded as asked: the corner at ``index``."""

    def __init__(self, index: int, problem: str):
        super().__init__(problem)
        self.index = index


def round_corners(corners: list[Point], radii: list[int]) -> list[tuple[Point, ...]]:
    """The closed outline through ``corners`` with every corner rounded by an arc of its radius;
    a radius of 0 leaves the corner as it is. The outline comes as its pieces in order: a
    straight one as ``(start, end)`` and an arc as ``(start, middle, end)``. The arc touches
    both edges of its corner, so each piece continues the one before without a kink.
    """
    count = len(corners)
    # For each corner: where the outline arrives, the middle of the arc, where the outline
    # leaves, and how much of either edge the arc takes up.
    joints = []
    for index, (corner, radius) in enumerate(zip(corners, radii, strict=True)):
        before, after = corners[index - 1], corners[(index + 1) % count]
        if corner == before:
            raise CornerError(index, "is given twice in a row")
        if radius <= 0:
            joints.append([corner, None, corner, 0.0])
            continue
        (ux, uy), (vx, vy) = _towards(corner, before), _towards(corner, after)
        half = math.acos(max(-1.0, min(1.0, ux * vx + uy * vy))) / 2
        if not 1e-6 < half < math.pi / 2 - 1e-6:
            raise CornerError(index, "lies on a straight line, which leaves no corner to round")
        cut = radius / math.tan(half)
        bisector = _towards((0, 0), (ux + vx, uy + vy))
        joints.append(
            [
                _along(corner, (ux, uy), cut),
                _along(corner, bisector, radius / math.sin(half) - radius),
                _along(corner, (vx, vy), cut),
                cut,
            ]
        )
    for index, joint in enumerate(joints):
        following = joints[(index + 1) % count]
        spare = math.dist(corners[index], corners[(index + 1) % count]) - joint[3] - following[3]
        if spare < -_SLACK:
            raise CornerError(index, "is too close to the next corner for the radii of the two")
        if spare <= _SLACK:
            following[0] = joint[2]  # two arcs meet: in one point, not in two a rounding apart
    pieces = []
    for index, (start, middle, end, _cut) in enumerate(joints):
        if middle is not None:
            pieces.append((start, middle, end))
        if end != joints[(index + 1) % count][0]:
            pieces.append((end, joints[(index + 1) % count][0]))
    return pieces


def _towards(start: Point, end: Point) -> tuple[float, float]:
    """The direction from ``start`` to ``end``, one unit long."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    return dx / length, dy / length


def _along(point: Point, direction: tuple[float, float], distance: float) -> Point:
    return round(point[0] + direction[0] * distance), round(point[1] + direction[1] * distance)


def point_in_polygon(point: Point, corners: list[Point]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside
