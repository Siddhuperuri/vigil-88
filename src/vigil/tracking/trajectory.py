"""Trajectory arithmetic: reported velocity, and the stationary test (04 §3.2, 05 §4).

Two decisions worth stating, because both are places where the obvious version is wrong.

**Velocity is a least-squares fit over matched points, not the Kalman filter's velocity state.**
The filter's velocity is tuned for association — it is deliberately responsive, because a
sluggish prediction loses tracks through occlusions — and reporting it would make an operator's
"how fast was that" answer depend on `kalman_acceleration_noise_ratio`. The fit depends only on
where the detector actually put the box and when. Interpolated points are excluded: a predicted
box carries no measurement, so including it would fit the filter's own extrapolation.

**Speed is expressed in box heights per second** wherever a threshold is applied, so one
threshold means the same thing for a subject near the camera and one far from it. Pixels per
second is still what is *reported*, because that is the honest unit without a ground plane
(04 §3.2); `velocity_m_s` stays `None` until a calibrated homography exists.
"""

from __future__ import annotations

from collections.abc import Sequence

from vigil.core.geometry import Vector2
from vigil.core.units import NS_PER_S
from vigil.domain.track import TrackPoint

MIN_SPAN_NS = 1  # a fit needs the points to span some time, however little


def fit_velocity_px_s(
    points: Sequence[TrackPoint], *, window_frames: int, min_points: int
) -> Vector2 | None:
    """Least-squares slope of the box centre against time, over the most recent measurements.

    `None` until `min_points` matched points exist, rather than 0.0: "not known yet" and "not
    moving" are different facts and a sentinel would conflate them (05 §1 rule 5).
    """
    measured = [p for p in points if not p.is_interpolated]
    if len(measured) < min_points:
        return None
    recent = measured[-window_frames:]
    if len(recent) < min_points:
        return None

    t0 = recent[0].t_monotonic_ns
    span_ns = recent[-1].t_monotonic_ns - t0
    if span_ns < MIN_SPAN_NS:
        return None

    # Times are seconds relative to the first point: absolute nanoseconds would lose precision
    # in the sums of squares.
    ts = [(p.t_monotonic_ns - t0) / NS_PER_S for p in recent]
    xs = [p.bbox.center.x for p in recent]
    ys = [p.bbox.center.y for p in recent]
    n = float(len(recent))
    mean_t = sum(ts) / n
    variance = sum((t - mean_t) ** 2 for t in ts)
    if variance <= 0.0:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    slope_x = sum((t - mean_t) * (x - mean_x) for t, x in zip(ts, xs, strict=True)) / variance
    slope_y = sum((t - mean_t) * (y - mean_y) for t, y in zip(ts, ys, strict=True)) / variance
    return Vector2(slope_x, slope_y)


def speed_box_heights_s(velocity: Vector2 | None, box_height_px: float) -> float | None:
    """Speed relative to the subject's own size: the unit every motion threshold uses."""
    if velocity is None:
        return None
    return velocity.magnitude / max(box_height_px, 1.0)
