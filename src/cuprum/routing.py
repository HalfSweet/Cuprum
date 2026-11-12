"""Geometry for hand routing: turning a list of waypoints into track segments that only run
horizontally, vertically or at 45 degrees and never turn a square corner.

Everything here works on plain ``(x, y)`` tuples in nanometres.
"""

from __future__ import annotations

import math

Point = tuple[int, int]

DIAGONAL_PLACES = ("middle", "start", "end")


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


def point_in_polygon(point: Point, corners: list[Point]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside
