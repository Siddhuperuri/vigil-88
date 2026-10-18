"""The calibration file format (Q4, 10 §8): `config/calibration/<calibration_ref>.calib.yaml`.

Four correspondences between pixels on a frame and positions measured on the floor. Like a zone
file this is validated on its own and never as part of `Settings`: a bad calibration must not
stop the application, it must leave that camera without `GROUND_PLANE` and say why (04 §8).

There is deliberately **no accuracy tolerance to set**. A four-point fit passes exactly
through its four points whatever they are, so a reprojection threshold would be a check that
can never fire. Degeneracy is caught here instead, by refusing points that coincide or lie
along a line, and in the solve itself.

**Pixels here are absolute, not normalized**, which is the opposite of a zone polygon and is
deliberate. A zone is a region of interest and survives a resolution change; a homography is a
map between two specific coordinate spaces, and the pixel one has a size. Writing the frame size
into the file means a calibration taken at 1920x1080 is **refused** for a 1280x720 stream
instead of silently scaling by a factor nobody checked.
"""

from __future__ import annotations

import math
import re
from typing import Literal, Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel

CALIBRATION_REF_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")

REQUIRED_POINTS = 4
# Two image points closer together than this are a double-click, not two measurements.
MIN_PIXEL_SEPARATION = 8.0
# Two world points this close are the same spot on the floor.
MIN_WORLD_SEPARATION_M = 0.05
# A quad whose triangles are this thin is effectively three collinear points: the solve is
# technically possible and the result is dominated by measurement error.
MIN_TRIANGLE_AREA_RATIO = 1e-3
MAX_WORLD_EXTENT_M = 1000.0


class CalibrationPointConfig(ConfigModel):
    """One click on a frame, and what was measured on the floor there."""

    pixel: tuple[float, float]
    world_m: tuple[float, float]
    label: str | None = None

    @model_validator(mode="after")
    def _finite_and_sane(self) -> Self:
        if any(v < 0.0 for v in self.pixel):
            raise ValueError("pixel coordinates cannot be negative")
        if any(abs(v) > MAX_WORLD_EXTENT_M for v in self.world_m):
            raise ValueError(
                f"world_m coordinates must be within +/-{MAX_WORLD_EXTENT_M:g} m of the origin; "
                "the origin is wherever you chose to measure from, not a map datum"
            )
        return self


def _triangle_area(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0


class CalibrationConfig(ConfigModel):
    """A camera's ground plane. Four points, the frame they were taken on, and the tolerance."""

    version: Literal[1] = 1
    width_px: int = Field(ge=16, le=16384)
    height_px: int = Field(ge=16, le=16384)
    points: tuple[CalibrationPointConfig, ...]
    description: str | None = None

    @model_validator(mode="after")
    def _four_usable_points(self) -> Self:
        if len(self.points) != REQUIRED_POINTS:
            raise ValueError(
                f"a calibration needs exactly {REQUIRED_POINTS} points: four corners of "
                "something flat you can measure, such as a floor tile or a parking bay"
            )
        for point in self.points:
            x, y = point.pixel
            if x > self.width_px or y > self.height_px:
                raise ValueError(
                    f"pixel ({x:g}, {y:g}) lies outside the stated frame of "
                    f"{self.width_px}x{self.height_px}"
                )
        pixels = [p.pixel for p in self.points]
        worlds = [p.world_m for p in self.points]
        for i in range(REQUIRED_POINTS):
            for j in range(i + 1, REQUIRED_POINTS):
                if _distance(pixels[i], pixels[j]) < MIN_PIXEL_SEPARATION:
                    raise ValueError(
                        f"points {i + 1} and {j + 1} are less than {MIN_PIXEL_SEPARATION:g} px "
                        "apart: they look like the same click"
                    )
                if _distance(worlds[i], worlds[j]) < MIN_WORLD_SEPARATION_M:
                    raise ValueError(
                        f"points {i + 1} and {j + 1} are less than {MIN_WORLD_SEPARATION_M:g} m "
                        "apart on the floor: they are the same spot"
                    )
        self._refuse_degenerate_quad(pixels, self.width_px * self.height_px, "pixel")
        world_extent = _extent(worlds)
        self._refuse_degenerate_quad(worlds, world_extent, "world_m")
        return self

    @staticmethod
    def _refuse_degenerate_quad(
        points: list[tuple[float, float]], reference_area: float, space: str
    ) -> None:
        """Every triangle of the quad must have real area, in both spaces.

        Checked in both because the failures differ: collinear *pixels* mean the operator clicked
        along an edge, collinear *world* points mean they measured along one.
        """
        if reference_area <= 0.0:
            raise ValueError(f"the {space} points span no area")
        for drop in range(REQUIRED_POINTS):
            triangle = [p for index, p in enumerate(points) if index != drop]
            ratio = _triangle_area(*triangle) / reference_area
            if ratio < MIN_TRIANGLE_AREA_RATIO:
                raise ValueError(
                    f"three of the four {space} points are nearly collinear "
                    f"(triangle area {ratio:.2%} of the span). Pick four corners of a shape "
                    "with real width and depth, not four points along a line"
                )


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _extent(points: list[tuple[float, float]]) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return max(max(xs) - min(xs), 1e-9) * max(max(ys) - min(ys), 1e-9)
