"""Pure geometry: no cv2, no numpy. Coordinates are floats in the caller's declared space."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.x) and math.isfinite(self.y)):
            raise ValueError("Point coordinates must be finite")

    def distance_to(self, other: Point) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True, slots=True)
class Vector2:
    x: float
    y: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.x) and math.isfinite(self.y)):
            raise ValueError("Vector2 components must be finite")

    @property
    def magnitude(self) -> float:
        return math.hypot(self.x, self.y)


@dataclass(frozen=True, slots=True)
class BBox:
    """Axis-aligned box, x1<=x2 and y1<=y2."""

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.x1, self.y1, self.x2, self.y2)):
            raise ValueError("BBox coordinates must be finite")
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError(f"BBox has negative extent: {self}")

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Point:
        return Point((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    @property
    def bottom_center(self) -> Point:
        """Foot point: the right anchor for 'is this person standing in that zone'."""
        return Point((self.x1 + self.x2) / 2, self.y2)

    def contains(self, p: Point) -> bool:
        return self.x1 <= p.x <= self.x2 and self.y1 <= p.y <= self.y2

    def intersection_area(self, other: BBox) -> float:
        w = min(self.x2, other.x2) - max(self.x1, other.x1)
        h = min(self.y2, other.y2) - max(self.y1, other.y1)
        return max(w, 0.0) * max(h, 0.0)

    def iou(self, other: BBox) -> float:
        inter = self.intersection_area(other)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def gap_to(self, other: BBox) -> float:
        """Shortest distance between the two boxes; 0 when they touch or overlap."""
        dx = max(self.x1 - other.x2, other.x1 - self.x2, 0.0)
        dy = max(self.y1 - other.y2, other.y1 - self.y2, 0.0)
        return math.hypot(dx, dy)

    def clipped(self, width: float, height: float) -> BBox:
        """Clamp into [0,width]x[0,height]."""
        return BBox(
            min(max(self.x1, 0.0), width),
            min(max(self.y1, 0.0), height),
            min(max(self.x2, 0.0), width),
            min(max(self.y2, 0.0), height),
        )

    def scaled(self, sx: float, sy: float) -> BBox:
        return BBox(self.x1 * sx, self.y1 * sy, self.x2 * sx, self.y2 * sy)


@dataclass(frozen=True, slots=True, init=False)
class Polygon:
    points: tuple[Point, ...]

    def __init__(self, points: Sequence[Point]) -> None:
        if len(points) < 3:
            raise ValueError("a Polygon needs at least 3 points")
        object.__setattr__(self, "points", tuple(points))

    def area(self) -> float:
        """Absolute shoelace area."""
        total = 0.0
        pts = self.points
        for i, a in enumerate(pts):
            b = pts[(i + 1) % len(pts)]
            total += a.x * b.y - b.x * a.y
        return abs(total) / 2

    def bounds(self) -> BBox:
        xs = [p.x for p in self.points]
        ys = [p.y for p in self.points]
        return BBox(min(xs), min(ys), max(xs), max(ys))

    def scaled(self, sx: float, sy: float) -> Polygon:
        return Polygon([Point(p.x * sx, p.y * sy) for p in self.points])

    def contains(self, p: Point) -> bool:
        """Even-odd ray casting. Points on an edge or vertex count as inside."""
        pts = self.points
        inside = False
        for i, a in enumerate(pts):
            b = pts[(i + 1) % len(pts)]
            if _on_segment(p, a, b):
                return True
            if (a.y > p.y) != (b.y > p.y):
                x_cross = a.x + (p.y - a.y) * (b.x - a.x) / (b.y - a.y)
                if p.x < x_cross:
                    inside = not inside
        return inside

    def distance_to_boundary(self, p: Point) -> float:
        """Distance from `p` to the nearest edge, whether `p` is inside or outside."""
        pts = self.points
        return min(_distance_to_segment(p, a, pts[(i + 1) % len(pts)]) for i, a in enumerate(pts))

    def is_simple(self) -> bool:
        """True if the outline never crosses or touches itself and encloses a real area.

        Even-odd containment is only meaningful for a simple polygon: a bow-tie has regions
        that are 'inside' by the rule and 'outside' by anyone's intuition, and zones are drawn
        by people.
        """
        pts = self.points
        n = len(pts)
        if self.area() <= _EPS:
            return False
        edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
        if any(a == b for a, b in edges):
            return False  # a zero-length edge (duplicated vertex)
        for i in range(n):
            for j in range(i + 1, n):
                adjacent = j == i + 1 or (i == 0 and j == n - 1)
                a1, a2 = edges[i]
                b1, b2 = edges[j]
                if adjacent:
                    # neighbours share exactly one vertex; they must not fold back over each other
                    shared, free_a, free_b = _shared_vertex(a1, a2, b1, b2)
                    if _on_segment(free_a, shared, free_b) or _on_segment(free_b, shared, free_a):
                        return False
                elif _segments_touch(a1, a2, b1, b2):
                    return False
        return True


def _shared_vertex(a1: Point, a2: Point, b1: Point, b2: Point) -> tuple[Point, Point, Point]:
    """For two edges that share a vertex: (shared, the other end of a, the other end of b)."""
    if a2 == b1:
        return a2, a1, b2
    if a1 == b2:
        return a1, a2, b1
    if a1 == b1:
        return a1, a2, b2
    return a2, a1, b1  # a2 == b2


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)


def _segments_touch(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """True if two segments share any point (proper crossing, touching, or collinear overlap)."""
    d1, d2 = _cross(b1, b2, a1), _cross(b1, b2, a2)
    d3, d4 = _cross(a1, a2, b1), _cross(a1, a2, b2)
    if ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and (
        (d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)
    ):
        return True
    return (
        _on_segment(a1, b1, b2)
        or _on_segment(a2, b1, b2)
        or _on_segment(b1, a1, a2)
        or _on_segment(b2, a1, a2)
    )


def _distance_to_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return p.distance_to(a)
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / length_sq))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    cross = (p.y - a.y) * (b.x - a.x) - (p.x - a.x) * (b.y - a.y)
    scale = max(abs(b.x - a.x), abs(b.y - a.y), 1.0)
    if abs(cross) > _EPS * scale * scale:
        return False
    return (
        min(a.x, b.x) - _EPS <= p.x <= max(a.x, b.x) + _EPS
        and min(a.y, b.y) - _EPS <= p.y <= max(a.y, b.y) + _EPS
    )
