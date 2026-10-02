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


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    cross = (p.y - a.y) * (b.x - a.x) - (p.x - a.x) * (b.y - a.y)
    scale = max(abs(b.x - a.x), abs(b.y - a.y), 1.0)
    if abs(cross) > _EPS * scale * scale:
        return False
    return (
        min(a.x, b.x) - _EPS <= p.x <= max(a.x, b.x) + _EPS
        and min(a.y, b.y) - _EPS <= p.y <= max(a.y, b.y) + _EPS
    )
