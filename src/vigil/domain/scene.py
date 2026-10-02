"""Zones and the scene snapshot (05 §4). The scene engine arrives in P2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from vigil.core.capabilities import Capability
from vigil.core.geometry import Polygon
from vigil.core.ids import validate_camera_id
from vigil.domain._validate import (
    frozen_mapping,
    require_non_empty,
    require_non_negative,
    require_ratio,
    require_utc,
)
from vigil.domain.enums import ObjectClass, SceneFact, SpatialRelationKind, ZoneKind
from vigil.domain.frame import FrameMeta
from vigil.domain.track import TrackedObject

MINUTES_PER_DAY = 24 * 60
MAX_CRITICALITY = 4


@dataclass(frozen=True, slots=True)
class Schedule:
    """Daily active window in minutes-of-day, evaluated at `utc_offset_minutes` from UTC.

    A window may wrap midnight (18:00-06:00 is start=1080, end=360). An empty `weekdays`
    set means every day. Weekdays are Monday=0 .. Sunday=6 in the *local* time.
    """

    start_minute: int
    end_minute: int
    utc_offset_minutes: int = 0
    weekdays: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        for name, v in (("start_minute", self.start_minute), ("end_minute", self.end_minute)):
            if not 0 <= v < MINUTES_PER_DAY:
                raise ValueError(f"{name} must be in [0, {MINUTES_PER_DAY})")
        if self.start_minute == self.end_minute:
            raise ValueError("Schedule window must not be empty; omit the schedule for 'always'")
        if any(not 0 <= d <= 6 for d in self.weekdays):
            raise ValueError("weekdays must be in 0..6")
        object.__setattr__(self, "weekdays", frozenset(self.weekdays))

    def is_active_at(self, wall_utc: datetime) -> bool:
        require_utc("wall_utc", wall_utc)
        from datetime import timedelta

        local = wall_utc + timedelta(minutes=self.utc_offset_minutes)
        if self.weekdays and local.weekday() not in self.weekdays:
            return False
        minute = local.hour * 60 + local.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute


@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: str
    camera_id: str
    kind: ZoneKind
    polygon: Polygon  # normalized [0,1] coordinates
    criticality: int
    active_schedule: Schedule | None
    label: str

    def __post_init__(self) -> None:
        require_non_empty("Zone.zone_id", self.zone_id)
        validate_camera_id(self.camera_id)
        if not 0 <= self.criticality <= MAX_CRITICALITY:
            raise ValueError(f"criticality must be in [0, {MAX_CRITICALITY}]")
        for p in self.polygon.points:
            if not (0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0):
                raise ValueError("Zone polygons use normalized coordinates in [0, 1]")

    def is_active_at(self, wall_utc: datetime) -> bool:
        return self.active_schedule is None or self.active_schedule.is_active_at(wall_utc)


@dataclass(frozen=True, slots=True)
class ZoneOccupancy:
    zone_id: str
    counts_by_class: Mapping[ObjectClass, int]
    track_ids: frozenset[int]
    density_per_kpx2: float
    entered_track_ids: frozenset[int]
    exited_track_ids: frozenset[int]

    def __post_init__(self) -> None:
        require_non_negative("density_per_kpx2", self.density_per_kpx2)
        object.__setattr__(self, "counts_by_class", frozen_mapping(self.counts_by_class))
        object.__setattr__(self, "track_ids", frozenset(self.track_ids))
        object.__setattr__(self, "entered_track_ids", frozenset(self.entered_track_ids))
        object.__setattr__(self, "exited_track_ids", frozenset(self.exited_track_ids))


@dataclass(frozen=True, slots=True)
class SpatialRelation:
    kind: SpatialRelationKind
    subject_track_id: int
    object_track_id: int | None
    zone_id: str | None
    distance_px: float | None

    def __post_init__(self) -> None:
        if (self.object_track_id is None) == (self.zone_id is None):
            raise ValueError("a relation targets exactly one of object_track_id or zone_id")


@dataclass(frozen=True, slots=True)
class SceneState:
    frame_meta: FrameMeta
    tracks: tuple[TrackedObject, ...]
    zones: tuple[Zone, ...]
    occupancy: Mapping[str, ZoneOccupancy]
    relations: tuple[SpatialRelation, ...]
    stability_ratio: float
    capabilities: frozenset[Capability]
    degraded_facts: frozenset[SceneFact]

    def __post_init__(self) -> None:
        require_ratio("stability_ratio", self.stability_ratio)
        object.__setattr__(self, "tracks", tuple(self.tracks))
        object.__setattr__(self, "zones", tuple(self.zones))
        object.__setattr__(self, "relations", tuple(self.relations))
        object.__setattr__(self, "occupancy", frozen_mapping(self.occupancy))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "degraded_facts", frozenset(self.degraded_facts))

    def track_by_id(self, track_id: int) -> TrackedObject:
        for t in self.tracks:
            if t.track_id == track_id:
                return t
        raise KeyError(track_id)
