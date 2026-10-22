"""Ground-plane calibration: four points, one homography, velocity in metres (Q4, 10 §8).

A fixed camera looking at a flat floor sees a projective transform of that floor. Four
correspondences between image pixels and measured ground positions determine it exactly, which
is why the configuration asks for four and not for a camera matrix: nobody calibrating a
corridor camera has a checkerboard, but anyone can measure the corners of a floor tile.

What this buys, and nothing more:

  * **positions and velocities on the calibrated plane.** A person's foot point is on the floor,
    so it maps. Their head is not, and this module will not pretend otherwise -- every
    conversion takes the point the caller nominates, and `scene/` nominates the foot point.
  * **a locally correct scale.** Perspective means metres-per-pixel is a function of position,
    not a constant. A velocity is converted through the homography's **Jacobian at that point**,
    which is the derivative of the exact map rather than an approximation of it.

What it does not buy:

  * **anything off the plane.** A thrown object, a person on a staircase, a vehicle on a ramp:
    the mapping is wrong for all of them, silently, because a projective map of a plane has no
    way to report that its input was not on the plane.
  * **accuracy beyond the measurements it was given.** The homography is exact through four
    points, so everything the operator mismeasured is in it, and `reprojection_error_px` cannot
    reveal that: it is ~0 by construction. It is **reported as a conditioning diagnostic and
    never used as a tolerance**, because a threshold on a number that is always zero would read
    as a safety check and be none. What does catch a degenerate quad is the singular-pivot test
    in the solve and the collinearity checks in the configuration schema.

No numpy: `03 §1` keeps the array backend optional and this is an 8x8 solve. The implementation
is Gaussian elimination with partial pivoting, which for this size is both exact enough and
easier to check than a dependency.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from vigil.config.schema.calibration import CalibrationConfig
from vigil.core.geometry import Point, Vector2

# Below this, the solve is singular: three of the four points are collinear, or two coincide.
_SINGULAR_PIVOT = 1e-12

# A homography whose third row sends a calibrated point to w ~= 0 maps it to infinity. That is
# geometrically the horizon, and no conversion there is meaningful.
_HORIZON_W = 1e-9

REQUIRED_POINTS = 4


class CalibrationError(ValueError):
    """The four correspondences do not determine a usable homography."""


@dataclass(frozen=True, slots=True)
class Correspondence:
    """One point whose pixel position and ground position are both known."""

    pixel: Point
    world_m: Point


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. Raises on a singular system."""
    size = len(rhs)
    # Work on an augmented copy so the caller's rows are untouched.
    rows = [[*matrix[i], rhs[i]] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot][column]) < _SINGULAR_PIVOT:
            raise CalibrationError(
                "the four points do not determine a homography: three of them are collinear, "
                "or two coincide"
            )
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = rows[column][column]
        for index in range(column, size + 1):
            rows[column][index] /= scale
        for other in range(size):
            if other == column:
                continue
            factor = rows[other][column]
            if factor == 0.0:
                continue
            for index in range(column, size + 1):
                rows[other][index] -= factor * rows[column][index]
    return [rows[i][size] for i in range(size)]


def _homography(points: Sequence[Correspondence]) -> tuple[float, ...]:
    """The 3x3 map from pixels to ground metres, row-major, normalised with h22 = 1.

    Fixing `h22 = 1` turns the usual 8x9 null-space problem into an 8x8 solve. It excludes
    exactly one family of homographies: those that send the ground origin to infinity, which
    would mean the operator placed the world origin on the horizon. That is not a calibration
    anyone means to express, and a degenerate input is reported rather than fitted.
    """
    if len(points) != REQUIRED_POINTS:
        raise CalibrationError(f"a homography needs exactly {REQUIRED_POINTS} correspondences")
    matrix: list[list[float]] = []
    rhs: list[float] = []
    for c in points:
        x, y = c.pixel.x, c.pixel.y
        u, v = c.world_m.x, c.world_m.y
        matrix.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        rhs.append(u)
        matrix.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        rhs.append(v)
    return (*_solve(matrix, rhs), 1.0)


@dataclass(frozen=True, slots=True)
class GroundPlane:
    """A calibrated floor. Immutable, thread-safe, and cheap to call on the analysis thread."""

    camera_id: str
    width_px: int
    height_px: int
    correspondences: tuple[Correspondence, ...]
    matrix: tuple[float, ...]
    reprojection_error_px: float
    description: str | None = None

    @classmethod
    def fit(
        cls,
        *,
        camera_id: str,
        width_px: int,
        height_px: int,
        correspondences: Sequence[Correspondence],
        description: str | None = None,
    ) -> GroundPlane:
        """Fit, or raise. There is no partially-usable calibration.

        The solve either determines a homography or it does not; `_solve` raises on the singular
        case. The reprojection error is then measured and carried for display, not compared
        against anything -- see the module docstring.
        """
        matrix = _homography(correspondences)
        blank = cls(
            camera_id=camera_id,
            width_px=width_px,
            height_px=height_px,
            correspondences=tuple(correspondences),
            matrix=matrix,
            reprojection_error_px=0.0,
            description=description,
        )
        return cls(
            camera_id=camera_id,
            width_px=width_px,
            height_px=height_px,
            correspondences=tuple(correspondences),
            matrix=matrix,
            reprojection_error_px=blank.measure_reprojection_error(),
            description=description,
        )

    def matches(self, width_px: int, height_px: int) -> bool:
        """A homography is in pixels, so it belongs to one frame size and no other."""
        return self.width_px == width_px and self.height_px == height_px

    # ---------------------------------------------------------------- conversions

    def to_world_m(self, pixel: Point) -> Point | None:
        """Ground position of a pixel, in metres. `None` at or beyond the horizon."""
        h = self.matrix
        w = h[6] * pixel.x + h[7] * pixel.y + h[8]
        if abs(w) < _HORIZON_W:
            return None
        return Point(
            (h[0] * pixel.x + h[1] * pixel.y + h[2]) / w,
            (h[3] * pixel.x + h[4] * pixel.y + h[5]) / w,
        )

    def jacobian_at(self, pixel: Point) -> tuple[float, float, float, float] | None:
        """d(world)/d(pixel) at one point, row-major, or `None` at the horizon.

        This is what makes the velocity conversion exact rather than a local guess: the
        derivative of the projective map, not a finite difference across two sampled points.
        """
        h = self.matrix
        w = h[6] * pixel.x + h[7] * pixel.y + h[8]
        if abs(w) < _HORIZON_W:
            return None
        u = h[0] * pixel.x + h[1] * pixel.y + h[2]
        v = h[3] * pixel.x + h[4] * pixel.y + h[5]
        inverse_square = 1.0 / (w * w)
        return (
            (h[0] * w - u * h[6]) * inverse_square,
            (h[1] * w - u * h[7]) * inverse_square,
            (h[3] * w - v * h[6]) * inverse_square,
            (h[4] * w - v * h[7]) * inverse_square,
        )

    def to_world_velocity(self, pixel: Point, velocity_px_s: Vector2) -> Vector2 | None:
        """Convert px/s to m/s **at a nominated point**, because the scale varies with position.

        A single metres-per-pixel figure for the whole frame would be wrong everywhere except
        where it was measured, and most wrong exactly where perspective is steepest.
        """
        jacobian = self.jacobian_at(pixel)
        if jacobian is None:
            return None
        a, b, c, d = jacobian
        return Vector2(
            a * velocity_px_s.x + b * velocity_px_s.y,
            c * velocity_px_s.x + d * velocity_px_s.y,
        )

    def metres_per_pixel_at(self, pixel: Point) -> float | None:
        """Local linear scale: the square root of the area scale. A diagnostic, not a conversion.

        Reported so an operator can see the perspective spread across their frame -- a ratio of
        ten between near and far is normal and means a px/s threshold would be meaningless.
        """
        jacobian = self.jacobian_at(pixel)
        if jacobian is None:
            return None
        a, b, c, d = jacobian
        determinant = abs(a * d - b * c)
        return math.sqrt(determinant) if determinant > 0.0 else None

    # ---------------------------------------------------------------- diagnostics

    def measure_reprojection_error(self) -> float:
        """Worst distance, in **pixels**, between a stated pixel and its world point mapped back.

        Measured in pixels rather than metres so the number means the same thing to an operator
        who clicked those points on a frame. For a plane built by `fit` this is ~0; it is
        informative only for a matrix that came from somewhere else.
        """
        worst = 0.0
        for c in self.correspondences:
            mapped = self.to_world_m(c.pixel)
            if mapped is None:
                return math.inf
            # Round-trip through the forward map only: an inverse would introduce its own error
            # and confuse "my points disagree" with "my inverse is ill-conditioned".
            jacobian = self.jacobian_at(c.pixel)
            if jacobian is None:
                return math.inf
            a, b, cc, d = jacobian
            determinant = a * d - b * cc
            if abs(determinant) < _SINGULAR_PIVOT:
                return math.inf
            dx_m = c.world_m.x - mapped.x
            dy_m = c.world_m.y - mapped.y
            # Push the world-space residual back into pixels through the inverse Jacobian, so
            # the tolerance is in the unit the operator can judge.
            worst = max(
                worst,
                math.hypot(
                    (d * dx_m - b * dy_m) / determinant,
                    (-cc * dx_m + a * dy_m) / determinant,
                ),
            )
        return worst

    def extent_m(self) -> tuple[float, float] | None:
        """Width and depth, in metres, of the calibrated quad. Context for a reported speed."""
        xs = [c.world_m.x for c in self.correspondences]
        ys = [c.world_m.y for c in self.correspondences]
        if not xs:
            return None
        return (max(xs) - min(xs), max(ys) - min(ys))

    def scale_spread(self) -> float | None:
        """Ratio of the largest to the smallest metres-per-pixel over the calibrated corners.

        A high number is not an error: it is how much perspective this camera has, and the
        reason a single scale factor was refused.
        """
        scales = [
            s for c in self.correspondences if (s := self.metres_per_pixel_at(c.pixel)) is not None
        ]
        if not scales or min(scales) <= 0.0:
            return None
        return max(scales) / min(scales)


def ground_plane_from_config(config: CalibrationConfig, camera_id: str) -> GroundPlane:
    """Fit the plane a validated calibration file describes.

    The schema has already refused a degenerate quad, a repeated point and a point outside the
    stated frame, so the only failure left for `fit` is a singular solve, which it raises.
    """
    return GroundPlane.fit(
        camera_id=camera_id,
        width_px=config.width_px,
        height_px=config.height_px,
        correspondences=[
            Correspondence(pixel=Point(*p.pixel), world_m=Point(*p.world_m)) for p in config.points
        ],
        description=config.description,
    )
