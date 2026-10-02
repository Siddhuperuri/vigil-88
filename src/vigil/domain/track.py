"""Tracked objects and trajectories (05 §4). The tracking engine arrives in P2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise

from vigil.core.geometry import BBox, Vector2
from vigil.core.ids import validate_camera_id
from vigil.domain._validate import frozen_mapping, require_non_negative, require_ratio
from vigil.domain.enums import ObjectClass, TrackFlag


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

    def __post_init__(self) -> None:
        validate_camera_id(self.camera_id)
        require_ratio("TrackedObject.confidence_ratio", self.confidence_ratio)
        for name in ("track_id", "tracker_epoch", "age_frames", "hits", "time_since_update_frames"):
            require_non_negative(name, getattr(self, name))
        if self.hits > self.age_frames:
            raise ValueError("hits cannot exceed age_frames")
        if self.last_seen_monotonic_ns < self.first_seen_monotonic_ns:
            raise ValueError("last_seen precedes first_seen")
        object.__setattr__(self, "zone_dwell_ms", frozen_mapping(self.zone_dwell_ms))
        object.__setattr__(self, "flags", frozenset(self.flags))

    @property
    def hit_ratio(self) -> float:
        """hits / age_frames: low means the track is mostly prediction (06 §6 validator 3)."""
        return self.hits / self.age_frames if self.age_frames else 0.0
