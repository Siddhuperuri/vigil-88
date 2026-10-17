"""Tracked objects and trajectories (05 §4)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise

from vigil.core.geometry import BBox, Vector2
from vigil.core.ids import validate_camera_id
from vigil.core.units import NS_PER_MS
from vigil.domain._validate import frozen_mapping, require_non_negative, require_ratio
from vigil.domain.enums import ObjectClass, TrackFlag

_FULL_TURN_DEG = 360.0


@dataclass(frozen=True, slots=True)
class TrackPoint:
    t_monotonic_ns: int
    bbox: BBox
    confidence_ratio: float
    is_interpolated: bool

    def __post_init__(self) -> None:
        require_non_negative("t_monotonic_ns", self.t_monotonic_ns)
        require_ratio("TrackPoint.confidence_ratio", self.confidence_ratio)


@dataclass(frozen=True, slots=True)
class Trajectory:
    """Bounded history. `with_point` returns a new Trajectory; the old one is untouched."""

    points: tuple[TrackPoint, ...]
    max_length: int

    def __post_init__(self) -> None:
        if self.max_length < 1:
            raise ValueError("Trajectory.max_length must be >= 1")
        pts = tuple(self.points)
        if len(pts) > self.max_length:
            raise ValueError("Trajectory exceeds max_length")
        if any(b.t_monotonic_ns < a.t_monotonic_ns for a, b in pairwise(pts)):
            raise ValueError("Trajectory points must be time-ordered")
        object.__setattr__(self, "points", pts)

    def __len__(self) -> int:
        return len(self.points)

    def with_point(self, point: TrackPoint) -> Trajectory:
        kept = (*self.points, point)[-self.max_length :]
        return Trajectory(kept, self.max_length)


@dataclass(frozen=True, slots=True)
class TrackedObject:
    track_id: int
    camera_id: str
    tracker_epoch: int
    object_class: ObjectClass
    bbox: BBox
    confidence_ratio: float
    first_seen_monotonic_ns: int
    last_seen_monotonic_ns: int
    age_frames: int
    hits: int
    time_since_update_frames: int
    trajectory: Trajectory
    velocity_px_s: Vector2 | None
    velocity_m_s: Vector2 | None
    zone_dwell_ms: Mapping[str, float]
    flags: frozenset[TrackFlag]
    # Start of the current stationary stretch; set exactly while STATIONARY is flagged. The
    # trajectory is bounded, so a duration longer than its window cannot be derived from it.
    stationary_since_monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        validate_camera_id(self.camera_id)
        require_ratio("TrackedObject.confidence_ratio", self.confidence_ratio)
        for name in ("track_id", "tracker_epoch", "age_frames", "hits", "time_since_update_frames"):
            require_non_negative(name, getattr(self, name))
        if self.hits > self.age_frames:
            raise ValueError("hits cannot exceed age_frames")
        if self.last_seen_monotonic_ns < self.first_seen_monotonic_ns:
            raise ValueError("last_seen precedes first_seen")
        if self.stationary_since_monotonic_ns is not None and not (
            self.first_seen_monotonic_ns
            <= self.stationary_since_monotonic_ns
            <= self.last_seen_monotonic_ns
        ):
            raise ValueError("stationary_since must lie within the track's observed lifetime")
        object.__setattr__(self, "zone_dwell_ms", frozen_mapping(self.zone_dwell_ms))
        object.__setattr__(self, "flags", frozenset(self.flags))

    @property
    def hit_ratio(self) -> float:
        """hits / age_frames: low means the track is mostly prediction (06 §6 validator 3)."""
        return self.hits / self.age_frames if self.age_frames else 0.0

    @property
    def is_visible(self) -> bool:
        """Matched to a detection in the latest update (as opposed to predicted through a gap)."""
        return self.time_since_update_frames == 0

    @property
    def speed_px_s(self) -> float | None:
        """Pixel-space speed. Honest about its unit: not metres, not corrected for perspective."""
        return None if self.velocity_px_s is None else self.velocity_px_s.magnitude

    @property
    def heading_deg(self) -> float | None:
        """Direction of travel in image space, degrees in [0, 360): 0 = right (+x), 90 = down
        (+y, image rows grow downward), 180 = left, 270 = up. None where a direction is not
        meaningful: velocity unknown, zero, or the track is flagged STATIONARY."""
        v = self.velocity_px_s
        if v is None or TrackFlag.STATIONARY in self.flags or v.magnitude == 0.0:
            return None
        return math.degrees(math.atan2(v.y, v.x)) % _FULL_TURN_DEG

    @property
    def stationary_duration_ms(self) -> float | None:
        """How long the track has been stationary as of its last observation, else None."""
        if self.stationary_since_monotonic_ns is None:
            return None
        return (self.last_seen_monotonic_ns - self.stationary_since_monotonic_ns) / NS_PER_MS
