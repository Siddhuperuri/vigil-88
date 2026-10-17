"""`SceneStateBuilder`: hand-build a SceneState in three lines (09 §12).

If writing a module's unit test needs more setup than this, the module contract is leaking and
the contract is wrong, not the test. So this builder exists to keep that honest: no pipeline, no
detector, no frames, no clock.

    scene = (
        SceneStateBuilder(camera_id="test-cam")
        .with_zone("yard", ZoneKind.RESTRICTED, criticality=3)
        .with_track(1, ObjectClass.PERSON, at=(0.7, 0.6), hits=5, in_zones=["yard"])
        .build()
    )
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from vigil.core.capabilities import Capability
from vigil.core.geometry import BBox, Point, Polygon, Vector2
from vigil.core.units import ms_to_ns
from vigil.domain.enums import ObjectClass, SceneFact, TrackFlag, ZoneKind
from vigil.domain.frame import FrameMeta
from vigil.domain.scene import SceneState, Schedule, SpatialRelation, Zone, ZoneOccupancy
from vigil.domain.track import TrackedObject, TrackPoint, Trajectory

DEFAULT_CAMERA = "test-cam"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
# Everything a fully-capable camera supplies, which is what a module should be tested against
# before capabilities are taken away one at a time.
FULL_CAPABILITIES = frozenset(
    {
        Capability.DETECTION,
        Capability.TRACKING,
        Capability.VELOCITY,
        Capability.ZONES,
        Capability.SCENE_STABILITY,
    }
)
# A square in the lower-right quadrant, the shape most of these tests want.
DEFAULT_POLYGON: tuple[tuple[float, float], ...] = (
    (0.5, 0.4),
    (0.95, 0.4),
    (0.95, 0.95),
    (0.5, 0.95),
)


@dataclass(slots=True)
class _TrackSpec:
    track_id: int
    object_class: ObjectClass
    at: tuple[float, float]
    size: tuple[float, float]
    hits: int
    age_frames: int
    time_since_update_frames: int
    in_zones: tuple[str, ...]
    dwell_ms: Mapping[str, float]
    velocity_px_s: tuple[float, float] | None
    flags: frozenset[TrackFlag]
    tracker_epoch: int
    confidence_ratio: float
    first_seen_ms: float
    stationary_since_ms: float | None


@dataclass(slots=True)
class SceneStateBuilder:
    """Mutable builder; `build()` produces an immutable `SceneState`."""

    camera_id: str = DEFAULT_CAMERA
    width_px: int = DEFAULT_WIDTH
    height_px: int = DEFAULT_HEIGHT
    t_ms: float = 0.0
    frame_index: int = 0
    stream_epoch: int = 1
    stability_ratio: float = 1.0
    capabilities: frozenset[Capability] = FULL_CAPABILITIES
    degraded_facts: frozenset[SceneFact] = frozenset()
    wall_epoch: datetime = DEFAULT_EPOCH
    _zones: list[Zone] = field(default_factory=list)
    _tracks: list[_TrackSpec] = field(default_factory=list)
    _relations: list[SpatialRelation] = field(default_factory=list)

    # ---------------------------------------------------------------- zones

    def with_zone(
        self,
        zone_id: str,
        kind: ZoneKind = ZoneKind.RESTRICTED,
        *,
        criticality: int = 0,
        polygon: Sequence[tuple[float, float]] = DEFAULT_POLYGON,
        schedule: Schedule | None = None,
        label: str | None = None,
    ) -> SceneStateBuilder:
        self._zones.append(
            Zone(
                zone_id=zone_id,
                camera_id=self.camera_id,
                kind=kind,
                polygon=Polygon([Point(x, y) for x, y in polygon]),
                criticality=criticality,
                active_schedule=schedule,
                label=label or zone_id,
            )
        )
        return self

    # ---------------------------------------------------------------- tracks

    def with_track(
        self,
        track_id: int,
        object_class: ObjectClass = ObjectClass.PERSON,
        *,
        at: tuple[float, float] = (0.7, 0.6),
        size: tuple[float, float] = (60.0, 160.0),
        hits: int = 5,
        age_frames: int | None = None,
        time_since_update_frames: int = 0,
        in_zones: Iterable[str] = (),
        dwell_ms: Mapping[str, float] | None = None,
        velocity_px_s: tuple[float, float] | None = None,
        flags: Iterable[TrackFlag] = (),
        tracker_epoch: int = 1,
        confidence_ratio: float = 0.9,
        first_seen_ms: float | None = None,
        stationary_since_ms: float | None = None,
    ) -> SceneStateBuilder:
        """`at` is the **foot point** in normalized coordinates: where the object stands.

        That matches the scene analyzer's default `zone_anchor`, so a track placed inside a
        zone polygon is one the intrusion module will also consider inside it.
        """
        zones = tuple(in_zones)
        self._tracks.append(
            _TrackSpec(
                track_id=track_id,
                object_class=object_class,
                at=at,
                size=size,
                hits=hits,
                age_frames=hits if age_frames is None else age_frames,
                time_since_update_frames=time_since_update_frames,
                in_zones=zones,
                dwell_ms=dict(dwell_ms or dict.fromkeys(zones, 0.0)),
                velocity_px_s=velocity_px_s,
                flags=frozenset(flags),
                tracker_epoch=tracker_epoch,
                confidence_ratio=confidence_ratio,
                first_seen_ms=self.t_ms if first_seen_ms is None else first_seen_ms,
                stationary_since_ms=stationary_since_ms,
            )
        )
        return self

    def with_relation(self, relation: SpatialRelation) -> SceneStateBuilder:
        self._relations.append(relation)
        return self

    # ---------------------------------------------------------------- scene-level knobs

    def at_time(self, t_ms: float, *, frame_index: int | None = None) -> SceneStateBuilder:
        self.t_ms = t_ms
        self.frame_index = self.frame_index + 1 if frame_index is None else frame_index
        return self

    def advance(self, dt_ms: float) -> SceneStateBuilder:
        return self.at_time(self.t_ms + dt_ms)

    def with_capabilities(self, capabilities: Iterable[Capability]) -> SceneStateBuilder:
        self.capabilities = frozenset(capabilities)
        return self

    def without_capability(self, capability: Capability) -> SceneStateBuilder:
        self.capabilities = self.capabilities - {capability}
        return self

    def with_degraded_fact(self, fact: SceneFact) -> SceneStateBuilder:
        self.degraded_facts = self.degraded_facts | {fact}
        return self

    def with_stability(self, ratio: float) -> SceneStateBuilder:
        self.stability_ratio = ratio
        return self

    def with_epoch(self, stream_epoch: int) -> SceneStateBuilder:
        self.stream_epoch = stream_epoch
        return self

    def copy(self) -> SceneStateBuilder:
        """A detached copy, so a scenario can fork without disturbing the original."""
        clone = replace(self)
        clone._zones = list(self._zones)
        clone._tracks = [replace(t) for t in self._tracks]
        clone._relations = list(self._relations)
        return clone

    # ---------------------------------------------------------------- build

    def frame_meta(self) -> FrameMeta:
        return FrameMeta(
            camera_id=self.camera_id,
            stream_epoch=self.stream_epoch,
            frame_index=self.frame_index,
            t_monotonic_ns=ms_to_ns(self.t_ms),
            wall_utc=self.wall_epoch.fromtimestamp(
                self.wall_epoch.timestamp() + self.t_ms / 1000.0, tz=UTC
            ),
            width_px=self.width_px,
            height_px=self.height_px,
            source_pts_ms=None,
            is_keyframe=None,
        )

    def _bbox(self, spec: _TrackSpec) -> BBox:
        foot_x = spec.at[0] * self.width_px
        foot_y = spec.at[1] * self.height_px
        w, h = spec.size
        return BBox(foot_x - w / 2.0, foot_y - h, foot_x + w / 2.0, foot_y)

    def _track(self, spec: _TrackSpec) -> TrackedObject:
        box = self._bbox(spec)
        points = (
            TrackPoint(
                t_monotonic_ns=ms_to_ns(spec.first_seen_ms),
                bbox=box,
                confidence_ratio=spec.confidence_ratio,
                is_interpolated=False,
            ),
            TrackPoint(
                t_monotonic_ns=ms_to_ns(self.t_ms),
                bbox=box,
                confidence_ratio=spec.confidence_ratio,
                is_interpolated=spec.time_since_update_frames > 0,
            ),
        )
        velocity = None if spec.velocity_px_s is None else Vector2(*spec.velocity_px_s)
        return TrackedObject(
            track_id=spec.track_id,
            camera_id=self.camera_id,
            tracker_epoch=spec.tracker_epoch,
            object_class=spec.object_class,
            bbox=box,
            confidence_ratio=spec.confidence_ratio,
            first_seen_monotonic_ns=ms_to_ns(spec.first_seen_ms),
            last_seen_monotonic_ns=ms_to_ns(self.t_ms),
            age_frames=spec.age_frames,
            hits=spec.hits,
            time_since_update_frames=spec.time_since_update_frames,
            trajectory=Trajectory(points if spec.first_seen_ms < self.t_ms else points[1:], 90),
            velocity_px_s=velocity,
            velocity_m_s=None,
            zone_dwell_ms=spec.dwell_ms,
            flags=spec.flags,
            stationary_since_monotonic_ns=(
                None if spec.stationary_since_ms is None else ms_to_ns(spec.stationary_since_ms)
            ),
        )

    def _occupancy(self) -> dict[str, ZoneOccupancy]:
        """Occupancy is declared per track via `in_zones`, not derived from geometry.

        Deriving it here would make these tests depend on the scene analyzer, which is exactly
        the coupling the SceneState boundary exists to prevent — and it would make a test of
        "occupancy says X but geometry says Y" impossible to write.
        """
        result: dict[str, ZoneOccupancy] = {}
        for zone in self._zones:
            members = [t for t in self._tracks if zone.zone_id in t.in_zones]
            counts: dict[ObjectClass, int] = {}
            for spec in members:
                counts[spec.object_class] = counts.get(spec.object_class, 0) + 1
            result[zone.zone_id] = ZoneOccupancy(
                zone_id=zone.zone_id,
                counts_by_class=counts,
                track_ids=frozenset(t.track_id for t in members),
                density_per_kpx2=0.0,
                entered_track_ids=frozenset(),
                exited_track_ids=frozenset(),
            )
        return result

    def build(self) -> SceneState:
        return SceneState(
            frame_meta=self.frame_meta(),
            tracks=tuple(self._track(t) for t in self._tracks),
            zones=tuple(self._zones),
            occupancy=self._occupancy(),
            relations=tuple(self._relations),
            stability_ratio=self.stability_ratio,
            capabilities=self.capabilities,
            degraded_facts=self.degraded_facts,
        )
