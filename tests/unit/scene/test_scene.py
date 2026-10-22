"""The scene engine's claims: one anchor rule, one enter/exit answer, honest degraded facts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from vigil.config.schema.modules import SceneConfig
from vigil.config.schema.zones import ScheduleConfig, ZoneConfig, ZoneSetConfig
from vigil.core.capabilities import Capability
from vigil.core.geometry import BBox, Point, Polygon, Vector2
from vigil.domain.enums import ObjectClass, SceneFact, SpatialRelationKind, TrackFlag, ZoneKind
from vigil.domain.frame import FrameMeta
from vigil.domain.scene import Zone
from vigil.domain.track import TrackedObject, TrackPoint, Trajectory
from vigil.scene.analyzer import SceneAnalyzer
from vigil.scene.occupancy import OccupancyTracker
from vigil.scene.relations import compute_relations, eligible
from vigil.scene.zones import ZoneGeometry, anchor_point, zone_from_config, zones_from_set

CAM = "test-cam"
WIDTH, HEIGHT = 640, 480
WALL = datetime(2026, 1, 1, 12, tzinfo=UTC)
# Lower-right quadrant in normalized coordinates.
YARD = ((0.5, 0.4), (0.95, 0.4), (0.95, 0.95), (0.5, 0.95))


def meta(index: int = 0, t_ms: float = 0.0, *, stream_epoch: int = 1) -> FrameMeta:
    return FrameMeta(
        camera_id=CAM,
        stream_epoch=stream_epoch,
        frame_index=index,
        t_monotonic_ns=int(t_ms * 1e6),
        wall_utc=WALL,
        width_px=WIDTH,
        height_px=HEIGHT,
        source_pts_ms=None,
        is_keyframe=None,
    )


def zone(
    zone_id: str = "yard",
    kind: ZoneKind = ZoneKind.RESTRICTED,
    polygon: Sequence[tuple[float, float]] = YARD,
    *,
    criticality: int = 3,
) -> Zone:
    return Zone(
        zone_id=zone_id,
        camera_id=CAM,
        kind=kind,
        polygon=Polygon([Point(x, y) for x, y in polygon]),
        criticality=criticality,
        active_schedule=None,
        label=zone_id,
    )


def track(
    track_id: int,
    *,
    foot: tuple[float, float],
    size: tuple[float, float] = (60.0, 160.0),
    hits: int = 5,
    age: int | None = None,
    velocity: tuple[float, float] | None = None,
    t_ms: float = 0.0,
    flags: Sequence[TrackFlag] = (),
    object_class: ObjectClass = ObjectClass.PERSON,
    time_since_update: int = 0,
) -> TrackedObject:
    fx, fy = foot
    w, h = size
    box = BBox(fx - w / 2, fy - h, fx + w / 2, fy)
    point = TrackPoint(int(t_ms * 1e6), box, 0.9, time_since_update > 0)
    return TrackedObject(
        track_id=track_id,
        camera_id=CAM,
        tracker_epoch=1,
        object_class=object_class,
        bbox=box,
        confidence_ratio=0.9,
        first_seen_monotonic_ns=0,
        last_seen_monotonic_ns=int(t_ms * 1e6),
        age_frames=hits if age is None else age,
        hits=hits,
        time_since_update_frames=time_since_update,
        trajectory=Trajectory((point,), 90),
        velocity_px_s=None if velocity is None else Vector2(*velocity),
        velocity_m_s=None,
        zone_dwell_ms={},
        flags=frozenset(flags),
    )


# ---------------------------------------------------------------- zones


def test_zone_from_config_carries_schedule_and_criticality() -> None:
    config = ZoneConfig(
        zone_id="dock",
        kind="restricted",
        polygon=YARD,
        criticality=2,
        schedule=ScheduleConfig(start="18:00", end="06:00"),
    )
    built = zone_from_config(config, CAM)
    assert built.kind is ZoneKind.RESTRICTED
    assert built.criticality == 2
    assert built.active_schedule is not None
    assert built.active_schedule.start_minute == 18 * 60
    assert built.active_schedule.end_minute == 6 * 60
    assert not built.is_active_at(WALL)  # noon, outside an overnight window


def test_zones_from_set_respects_the_cap() -> None:
    zones = tuple(ZoneConfig(zone_id=f"z{i}", kind="counting", polygon=YARD) for i in range(5))
    built = zones_from_set(ZoneSetConfig(zones=zones), CAM, max_zones=3)
    assert [z.zone_id for z in built] == ["z0", "z1", "z2"]


def test_containment_uses_the_foot_point_not_box_overlap() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    config = SceneConfig()
    # Standing left of the zone, but the box is tall enough to overlap it.
    outside = track(1, foot=(300.0, 300.0), size=(200.0, 200.0))
    assert not geometry.contains("yard", anchor_point(outside, config))
    inside = track(2, foot=(400.0, 300.0))
    assert geometry.contains("yard", anchor_point(inside, config))


def test_fire_uses_the_centre_anchor_because_flames_have_no_footprint() -> None:
    config = SceneConfig()
    flame = track(1, foot=(400.0, 500.0), size=(40.0, 100.0), object_class=ObjectClass.FIRE)
    assert anchor_point(flame, config) == flame.bbox.center
    person = track(2, foot=(400.0, 500.0), size=(40.0, 100.0))
    assert anchor_point(person, config) == person.bbox.bottom_center


def test_geometry_is_rebuilt_when_the_frame_size_changes() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    assert geometry.matches(WIDTH, HEIGHT)
    assert not geometry.matches(1920, 1080)


def test_normalized_polygons_round_trip_into_pixels() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    polygon = geometry.polygon_px("yard")
    assert polygon is not None
    bounds = polygon.bounds()
    assert bounds.x1 == pytest.approx(0.5 * WIDTH, abs=0.5)
    assert bounds.y2 == pytest.approx(0.95 * HEIGHT, abs=0.5)


# ---------------------------------------------------------------- occupancy


def test_enter_and_exit_are_diffed_once_against_the_previous_frame() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracker = OccupancyTracker()
    config = SceneConfig()

    outside = tracker.update([track(1, foot=(100.0, 300.0))], geometry, config, t_monotonic_ns=0)
    assert outside.occupancy["yard"].track_ids == frozenset()

    entered = tracker.update(
        [track(1, foot=(400.0, 300.0), t_ms=100.0)], geometry, config, t_monotonic_ns=int(1e8)
    )
    assert entered.occupancy["yard"].entered_track_ids == frozenset({1})
    assert entered.occupancy["yard"].exited_track_ids == frozenset()

    stayed = tracker.update(
        [track(1, foot=(410.0, 300.0), t_ms=200.0)], geometry, config, t_monotonic_ns=int(2e8)
    )
    assert stayed.occupancy["yard"].entered_track_ids == frozenset()

    left = tracker.update(
        [track(1, foot=(100.0, 300.0), t_ms=300.0)], geometry, config, t_monotonic_ns=int(3e8)
    )
    assert left.occupancy["yard"].exited_track_ids == frozenset({1})


def test_a_track_that_vanishes_inside_a_zone_counts_as_an_exit() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracker = OccupancyTracker()
    config = SceneConfig()
    tracker.update([track(1, foot=(400.0, 300.0))], geometry, config, t_monotonic_ns=0)
    gone = tracker.update([], geometry, config, t_monotonic_ns=int(1e8))
    assert gone.occupancy["yard"].exited_track_ids == frozenset({1})


def test_dwell_reads_zero_on_entry_then_accumulates() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracker = OccupancyTracker()
    config = SceneConfig()
    first = tracker.update([track(1, foot=(400.0, 300.0))], geometry, config, t_monotonic_ns=0)
    assert first.dwell_ms_by_track[1]["yard"] == 0.0
    second = tracker.update(
        [track(1, foot=(400.0, 300.0), t_ms=100.0)], geometry, config, t_monotonic_ns=int(1e8)
    )
    assert second.dwell_ms_by_track[1]["yard"] == pytest.approx(100.0)
    third = tracker.update(
        [track(1, foot=(400.0, 300.0), t_ms=250.0)], geometry, config, t_monotonic_ns=int(2.5e8)
    )
    assert third.dwell_ms_by_track[1]["yard"] == pytest.approx(250.0)


def test_dwell_resets_when_the_subject_leaves() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracker = OccupancyTracker()
    config = SceneConfig()
    for i in range(4):
        tracker.update(
            [track(1, foot=(400.0, 300.0), t_ms=i * 100.0)],
            geometry,
            config,
            t_monotonic_ns=int(i * 1e8),
        )
    tracker.update(
        [track(1, foot=(100.0, 300.0), t_ms=400.0)], geometry, config, t_monotonic_ns=int(4e8)
    )
    back = tracker.update(
        [track(1, foot=(400.0, 300.0), t_ms=500.0)], geometry, config, t_monotonic_ns=int(5e8)
    )
    assert back.dwell_ms_by_track[1]["yard"] == 0.0


def test_dwell_for_a_departed_track_is_forgotten() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracker = OccupancyTracker()
    config = SceneConfig()
    tracker.update([track(1, foot=(400.0, 300.0))], geometry, config, t_monotonic_ns=0)
    result = tracker.update([], geometry, config, t_monotonic_ns=int(1e8))
    assert 1 not in result.dwell_ms_by_track


def test_occupancy_counts_by_class_and_reports_density() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracker = OccupancyTracker()
    result = tracker.update(
        [
            track(1, foot=(400.0, 300.0)),
            track(2, foot=(420.0, 320.0)),
            track(3, foot=(440.0, 340.0), object_class=ObjectClass.OBJECT_BAG),
        ],
        geometry,
        SceneConfig(),
        t_monotonic_ns=0,
    )
    occupancy = result.occupancy["yard"]
    assert occupancy.counts_by_class[ObjectClass.PERSON] == 2
    assert occupancy.counts_by_class[ObjectClass.OBJECT_BAG] == 1
    assert occupancy.density_per_kpx2 > 0.0


def test_reset_clears_membership_and_dwell() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracker = OccupancyTracker()
    config = SceneConfig()
    tracker.update([track(1, foot=(400.0, 300.0))], geometry, config, t_monotonic_ns=0)
    tracker.reset()
    after = tracker.update(
        [track(1, foot=(400.0, 300.0), t_ms=100.0)], geometry, config, t_monotonic_ns=int(1e8)
    )
    assert after.occupancy["yard"].entered_track_ids == frozenset({1})
    assert after.dwell_ms_by_track[1]["yard"] == 0.0


# ---------------------------------------------------------------- relations


def test_inside_and_near_are_reported() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    tracks = [track(1, foot=(400.0, 300.0)), track(2, foot=(420.0, 300.0))]
    result = compute_relations(tracks, geometry, SceneConfig())
    kinds = {r.kind for r in result.relations}
    assert SpatialRelationKind.INSIDE in kinds
    assert SpatialRelationKind.NEAR in kinds
    assert not result.degraded


def test_approaching_needs_closing_motion() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    config = SceneConfig()
    near_edge = track(1, foot=(300.0, 300.0), velocity=(60.0, 0.0))
    approaching = compute_relations([near_edge], geometry, config).relations
    assert any(r.kind is SpatialRelationKind.APPROACHING for r in approaching)

    receding = track(1, foot=(300.0, 300.0), velocity=(-60.0, 0.0))
    assert not any(
        r.kind is SpatialRelationKind.APPROACHING
        for r in compute_relations([receding], geometry, config).relations
    )


def test_converging_needs_both_velocities() -> None:
    geometry = ZoneGeometry([], width_px=WIDTH, height_px=HEIGHT)
    config = SceneConfig()
    a = track(1, foot=(100.0, 300.0), velocity=(50.0, 0.0))
    b = track(2, foot=(500.0, 300.0), velocity=(-50.0, 0.0))
    assert any(
        r.kind is SpatialRelationKind.CONVERGING
        for r in compute_relations([a, b], geometry, config).relations
    )
    no_velocity = track(2, foot=(500.0, 300.0))
    assert not any(
        r.kind is SpatialRelationKind.CONVERGING
        for r in compute_relations([a, no_velocity], geometry, config).relations
    )


def test_only_confirmed_visible_tracks_take_part() -> None:
    new = track(1, foot=(400.0, 300.0), hits=1, flags=[TrackFlag.NEW])
    occluded = track(2, foot=(400.0, 300.0), time_since_update=2)
    good = track(3, foot=(400.0, 300.0))
    assert [t.track_id for t in eligible([new, occluded, good])] == [3]


def test_relations_are_skipped_above_the_track_cap() -> None:
    geometry = ZoneGeometry([zone()], width_px=WIDTH, height_px=HEIGHT)
    config = SceneConfig(max_relation_tracks=2)
    tracks = [track(i, foot=(400.0 + i, 300.0)) for i in range(1, 5)]
    result = compute_relations(tracks, geometry, config)
    assert result.degraded
    assert result.relations == ()


# ---------------------------------------------------------------- the analyzer


def test_analyzer_copies_dwell_onto_the_tracks_it_reports() -> None:
    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig(), zones=[zone()])
    analyzer.analyze([track(1, foot=(400.0, 300.0))], meta(0, 0.0))
    scene = analyzer.analyze([track(1, foot=(400.0, 300.0), t_ms=100.0)], meta(1, 100.0))
    assert scene.track_by_id(1).zone_dwell_ms["yard"] == pytest.approx(100.0)


def test_analyzer_reports_missing_facts_rather_than_faking_them() -> None:
    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig(), zones=[])
    scene = analyzer.analyze([track(1, foot=(400.0, 300.0))], meta())
    assert SceneFact.ZONE_OCCUPANCY in scene.degraded_facts
    assert SceneFact.STABILITY in scene.degraded_facts  # no probe injected
    assert SceneFact.VELOCITY in scene.degraded_facts
    assert scene.stability_ratio == 1.0  # declared unmeasured, not claimed unstable


def test_velocity_is_not_degraded_once_a_track_reports_one() -> None:
    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig(), zones=[zone()])
    scene = analyzer.analyze([track(1, foot=(400.0, 300.0), velocity=(10.0, 0.0))], meta())
    assert SceneFact.VELOCITY not in scene.degraded_facts


def test_analyzer_passes_capabilities_through_untouched() -> None:
    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig(), zones=[zone()])
    granted = frozenset({Capability.DETECTION, Capability.TRACKING, Capability.ZONES})
    scene = analyzer.analyze([], meta(), capabilities=granted)
    assert scene.capabilities == granted


def test_editing_zones_takes_effect_without_a_restart_and_drops_stale_membership() -> None:
    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig(), zones=[zone()])
    analyzer.analyze([track(1, foot=(400.0, 300.0))], meta(0, 0.0))
    analyzer.set_zones([zone("dock", polygon=((0.0, 0.0), (0.3, 0.0), (0.3, 0.3), (0.0, 0.3)))])
    scene = analyzer.analyze([track(1, foot=(400.0, 300.0), t_ms=100.0)], meta(1, 100.0))
    assert [z.zone_id for z in scene.zones] == ["dock"]
    assert scene.occupancy["dock"].track_ids == frozenset()


def test_a_stability_probe_result_is_reported_verbatim() -> None:
    class Probe:
        def __init__(self) -> None:
            self.calls = 0

        def measure(self, pixels: object) -> float | None:
            self.calls += 1
            return 0.42

        def reset(self) -> None:
            self.calls = 0

    class Pixels:
        width_px = 2
        height_px = 2
        channels = 3
        nbytes = 12

        def tobytes(self) -> bytes:
            return bytes(12)

        def buffer(self) -> memoryview:
            return memoryview(bytes(12))

    probe = Probe()
    analyzer = SceneAnalyzer(camera_id=CAM, config=SceneConfig(), zones=[zone()], stability=probe)
    scene = analyzer.analyze([], meta(), pixels=Pixels())
    assert scene.stability_ratio == pytest.approx(0.42)
    assert SceneFact.STABILITY not in scene.degraded_facts
    assert probe.calls == 1
