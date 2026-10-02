from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigil.core.geometry import BBox, Point, Polygon, Vector2

coord = st.floats(min_value=-1e4, max_value=1e4, allow_nan=False, allow_infinity=False)


@st.composite
def boxes(draw: st.DrawFn) -> BBox:
    x1, y1 = draw(coord), draw(coord)
    return BBox(x1, y1, x1 + draw(st.floats(0, 1e3)), y1 + draw(st.floats(0, 1e3)))


def test_bbox_rejects_negative_extent_and_non_finite() -> None:
    with pytest.raises(ValueError, match="negative extent"):
        BBox(5, 0, 1, 1)
    with pytest.raises(ValueError, match="finite"):
        BBox(0, 0, math.inf, 1)
    with pytest.raises(ValueError, match="finite"):
        Point(math.nan, 0)
    with pytest.raises(ValueError, match="finite"):
        Vector2(0, math.inf)


def test_bbox_basics() -> None:
    b = BBox(10, 20, 50, 100)
    assert (b.width, b.height, b.area) == (40, 80, 3200)
    assert b.center == Point(30, 60)
    assert b.bottom_center == Point(30, 100)
    assert b.contains(Point(10, 20)) and not b.contains(Point(9.9, 50))


def test_iou_known_values() -> None:
    a = BBox(0, 0, 10, 10)
    assert a.iou(a) == 1.0
    assert a.iou(BBox(20, 20, 30, 30)) == 0.0
    assert a.iou(BBox(5, 0, 15, 10)) == pytest.approx(50 / 150)


def test_iou_of_two_degenerate_boxes_is_zero_not_nan() -> None:
    z = BBox(1, 1, 1, 1)
    assert z.iou(z) == 0.0


@given(boxes(), boxes())
def test_iou_is_symmetric_and_bounded(a: BBox, b: BBox) -> None:
    assert a.iou(b) == pytest.approx(b.iou(a))
    assert 0.0 <= a.iou(b) <= 1.0 + 1e-9


def test_clip_and_scale() -> None:
    assert BBox(-5, -5, 700, 500).clipped(640, 480) == BBox(0, 0, 640, 480)
    assert BBox(1, 2, 3, 4).scaled(2, 3) == BBox(2, 6, 6, 12)


SQUARE = Polygon([Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)])
L_SHAPE = Polygon(
    [Point(0, 0), Point(10, 0), Point(10, 4), Point(4, 4), Point(4, 10), Point(0, 10)]
)


def test_polygon_needs_three_points() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        Polygon([Point(0, 0), Point(1, 1)])


@pytest.mark.parametrize(
    ("p", "inside"),
    [
        (Point(5, 5), True),
        (Point(11, 5), False),
        (Point(-0.1, 5), False),
        (Point(0, 0), True),  # vertex
        (Point(5, 0), True),  # edge
        (Point(10, 10), True),
    ],
)
def test_square_containment(p: Point, inside: bool) -> None:
    assert SQUARE.contains(p) is inside


@pytest.mark.parametrize(
    ("p", "inside"), [(Point(2, 8), True), (Point(8, 2), True), (Point(8, 8), False)]
)
def test_concave_containment(p: Point, inside: bool) -> None:
    assert L_SHAPE.contains(p) is inside


def test_polygon_area_and_bounds() -> None:
    assert SQUARE.area() == 100
    assert L_SHAPE.area() == 64
    assert L_SHAPE.bounds() == BBox(0, 0, 10, 10)


@given(
    st.floats(0.1, 10), st.floats(0.1, 10), st.floats(0, 10), st.floats(0, 10)
)
def test_containment_survives_scaling(sx: float, sy: float, px: float, py: float) -> None:
    p = Point(px, py)
    scaled = SQUARE.scaled(sx, sy)
    assert SQUARE.contains(p) == scaled.contains(Point(px * sx, py * sy))


def test_vector_magnitude() -> None:
    assert Vector2(3, 4).magnitude == 5
