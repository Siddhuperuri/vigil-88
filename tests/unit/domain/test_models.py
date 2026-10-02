from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tests.support.fakes import StubPixels
from vigil.core.capabilities import Capability
from vigil.core.geometry import BBox, Point, Polygon
from vigil.domain import (
    AccumulatorState,
    Alert,
    AlertDelivery,
    Camera,
    CameraHealth,
    CameraState,
    CandidateEvent,
    Detection,
    DetectionSet,
    EventType,
    Evidence,
    EvidenceKind,
    Frame,
    FrameMeta,
    IncidentExplanation,
    ModelDescriptor,
    ObjectClass,
    Observation,
    SceneState,
    Schedule,
    Severity,
    SourceKind,
    SourceSpec,
    SubjectKey,
    SystemMetric,
    TrackedObject,
    TrackPoint,
    Trajectory,
    ValidatorOutcome,
    ValidatorVerdict,
    VerifiedEvent,
    Zone,
    ZoneKind,
    ZoneOccupancy,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID2 = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
MODEL = ModelDescriptor("null", "1", "null", None, None, "none", "none", None)


def meta(**kw: object) -> FrameMeta:
    base: dict[str, object] = dict(
        camera_id="cam-a",
        stream_epoch=1,
        frame_index=0,
        t_monotonic_ns=0,
        wall_utc=T0,
        width_px=64,
        height_px=48,
        source_pts_ms=None,
        is_keyframe=None,
    )
    return FrameMeta(**{**base, **kw})  # type: ignore[arg-type]


# -------- frame


def test_frame_identity_key() -> None:
    assert meta(stream_epoch=3, frame_index=9).key == ("cam-a", 3, 9)


@pytest.mark.parametrize(
    "bad",
    [
        {"camera_id": "BAD ID"},
        {"stream_epoch": -1},
        {"frame_index": -1},
        {"t_monotonic_ns": -5},
        {"width_px": 0},
        {"height_px": -1},
        {"source_pts_ms": -1.0},
        {"wall_utc": datetime(2026, 1, 1)},  # noqa: DTZ001 - naive on purpose
        {"wall_utc": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))},
    ],
)
def test_frame_meta_rejects_invalid_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        meta(**bad)


def test_frame_checks_pixels_match_meta() -> None:
    Frame(meta(), StubPixels(64, 48))
    with pytest.raises(ValueError, match="pixel buffer"):
        Frame(meta(), StubPixels(32, 48))


def test_frozen_dataclasses_cannot_be_mutated() -> None:
    m = meta()
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.frame_index = 5  # type: ignore[misc]


# -------- detection


def test_detection_confidence_must_be_a_ratio() -> None:
    box = BBox(0, 0, 1, 1)
    Detection(box, ObjectClass.PERSON, 1.0, "person")
    for bad in (-0.01, 1.01, float("nan")):
        with pytest.raises(ValueError):
            Detection(box, ObjectClass.PERSON, bad, "person")


def test_detection_set_may_be_empty_and_is_tuple() -> None:
    ds = DetectionSet(meta(), [], MODEL, 0.0)  # type: ignore[arg-type]
    assert ds.detections == () and isinstance(ds.detections, tuple)
    with pytest.raises(ValueError):
        DetectionSet(meta(), (), MODEL, -1.0)


def test_model_descriptor_requires_name_and_backend() -> None:
    with pytest.raises(ValueError):
        ModelDescriptor("", "1", "null", None, None, "none", "none", None)
    with pytest.raises(ValueError):
        ModelDescriptor("n", "1", " ", None, None, "none", "none", None)


# -------- tracks


def tp(t: int) -> TrackPoint:
    return TrackPoint(t, BBox(0, 0, 1, 1), 0.9, False)


def test_trajectory_is_bounded_and_immutable() -> None:
    t = Trajectory((), max_length=3)
    for i in range(5):
        t = t.with_point(tp(i))
    assert [p.t_monotonic_ns for p in t.points] == [2, 3, 4] and len(t) == 3
    original = Trajectory((tp(0),), 3)
    original.with_point(tp(1))
    assert len(original) == 1


def test_trajectory_validates_order_and_bound() -> None:
    with pytest.raises(ValueError, match="time-ordered"):
        Trajectory((tp(5), tp(1)), 5)
    with pytest.raises(ValueError, match="max_length"):
        Trajectory((tp(1), tp(2)), 1)
    with pytest.raises(ValueError):
        Trajectory((), 0)


def track(**kw: object) -> TrackedObject:
    base: dict[str, object] = dict(
        track_id=1,
        camera_id="cam-a",
        tracker_epoch=1,
        object_class=ObjectClass.PERSON,
        bbox=BBox(0, 0, 10, 10),
        confidence_ratio=0.9,
        first_seen_monotonic_ns=0,
        last_seen_monotonic_ns=10,
        age_frames=10,
        hits=8,
        time_since_update_frames=0,
        trajectory=Trajectory((), 5),
        velocity_px_s=None,
        velocity_m_s=None,
        zone_dwell_ms={},
        flags=frozenset(),
    )
    return TrackedObject(**{**base, **kw})  # type: ignore[arg-type]


def test_track_hit_ratio_and_invariants() -> None:
    assert track().hit_ratio == 0.8
    assert track(age_frames=0, hits=0).hit_ratio == 0.0
    with pytest.raises(ValueError, match="hits"):
        track(hits=11)
    with pytest.raises(ValueError, match="precedes"):
        track(first_seen_monotonic_ns=20, last_seen_monotonic_ns=10)


def test_track_mappings_are_read_only_copies() -> None:
    src = {"yard": 12.0}
    t = track(zone_dwell_ms=src)
    src["yard"] = 99.0
    assert t.zone_dwell_ms["yard"] == 12.0
    with pytest.raises(TypeError):
        t.zone_dwell_ms["x"] = 1.0  # type: ignore[index]


# -------- scene


@pytest.mark.parametrize(
    ("hour", "expected"), [(17, False), (18, True), (23, True), (3, True), (5, True), (6, False)]
)
def test_overnight_schedule_wraps_midnight(hour: int, expected: bool) -> None:
    s = Schedule(start_minute=18 * 60, end_minute=6 * 60)
    assert s.is_active_at(datetime(2026, 1, 1, hour, 30, tzinfo=UTC)) is expected


def test_schedule_respects_offset_and_weekdays() -> None:
    s = Schedule(
        9 * 60, 17 * 60, utc_offset_minutes=120, weekdays=frozenset({0})
    )  # Mon 09-17 UTC+2
    assert s.is_active_at(datetime(2026, 1, 5, 8, 0, tzinfo=UTC))  # Monday 10:00 local
    assert not s.is_active_at(datetime(2026, 1, 5, 16, 0, tzinfo=UTC))  # 18:00 local
    assert not s.is_active_at(datetime(2026, 1, 6, 8, 0, tzinfo=UTC))  # Tuesday


def test_schedule_validation() -> None:
    with pytest.raises(ValueError):
        Schedule(10, 10)
    with pytest.raises(ValueError):
        Schedule(-1, 10)
    with pytest.raises(ValueError):
        Schedule(0, 10, weekdays=frozenset({7}))


SQUARE = Polygon([Point(0.1, 0.1), Point(0.9, 0.1), Point(0.9, 0.9), Point(0.1, 0.9)])


def test_zone_uses_normalised_coordinates_and_schedule() -> None:
    z = Zone("yard", "cam-a", ZoneKind.RESTRICTED, SQUARE, 3, None, "Yard")
    assert z.is_active_at(T0)
    timed = dataclasses.replace(z, active_schedule=Schedule(18 * 60, 6 * 60))
    assert not timed.is_active_at(datetime(2026, 1, 1, 12, tzinfo=UTC))
    with pytest.raises(ValueError, match="normalized"):
        Zone(
            "z",
            "cam-a",
            ZoneKind.RESTRICTED,
            Polygon([Point(0, 0), Point(5, 0), Point(5, 5)]),
            1,
            None,
            "x",
        )
    with pytest.raises(ValueError, match="criticality"):
        Zone("z", "cam-a", ZoneKind.RESTRICTED, SQUARE, 9, None, "x")


def scene(**kw: object) -> SceneState:
    base: dict[str, object] = dict(
        frame_meta=meta(),
        tracks=(track(track_id=7),),
        zones=(),
        occupancy={},
        relations=(),
        stability_ratio=1.0,
        capabilities=frozenset({Capability.DETECTION}),
        degraded_facts=frozenset(),
    )
    return SceneState(**{**base, **kw})  # type: ignore[arg-type]


def test_scene_lookup_and_validation() -> None:
    s = scene()
    assert s.track_by_id(7).track_id == 7
    with pytest.raises(KeyError):
        s.track_by_id(8)
    with pytest.raises(ValueError):
        scene(stability_ratio=1.5)


def test_zone_occupancy_freezes_collections() -> None:
    occ = ZoneOccupancy("yard", {ObjectClass.PERSON: 1}, {1}, 0.0, set(), set())  # type: ignore[arg-type]
    assert isinstance(occ.track_ids, frozenset)
    with pytest.raises(TypeError):
        occ.counts_by_class[ObjectClass.PERSON] = 2  # type: ignore[index]


# -------- observation / events


def test_observation_is_a_ratio_signal_not_a_probability_of_an_incident() -> None:
    obs = Observation("cam-a", "intrusion", SubjectKey("track", "42"), 0.7, 5, {"zone": "yard"})
    assert str(obs.subject) == "track:42" and obs.signal == 0.7
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError):
            Observation("cam-a", "m", SubjectKey("camera", ""), bad, 0, {})


def test_subject_key_forms() -> None:
    assert str(SubjectKey("camera", "")) == "camera:"
    with pytest.raises(ValueError):
        SubjectKey("track", "")


def acc() -> AccumulatorState:
    return AccumulatorState(
        "cam-a", "intrusion", SubjectKey("track", "1"), 2.4, 0.92, 9, 0.9, 100, 200, True
    )


def test_accumulator_state_validation() -> None:
    assert acc().is_active
    with pytest.raises(ValueError):
        dataclasses.replace(acc(), confidence_ratio=2.0)
    with pytest.raises(ValueError, match="precedes"):
        dataclasses.replace(acc(), first_observation_ns=300)


def candidate() -> CandidateEvent:
    return CandidateEvent(
        ULID,
        "cam-a",
        "intrusion",
        EventType.INTRUSION,
        SubjectKey("track", "1"),
        acc(),
        [],
        {1},
        {"yard"},
        5,
        T0,
    )  # type: ignore[arg-type]


def test_candidate_requires_ulid_and_freezes_collections() -> None:
    c = candidate()
    assert isinstance(c.observations, tuple) and isinstance(c.zone_ids, frozenset)
    with pytest.raises(ValueError, match="ULID"):
        dataclasses.replace(c, candidate_id="nope")


def test_verified_event_cannot_carry_a_rejection() -> None:
    ok = ValidatorVerdict("camera_health", ValidatorOutcome.PASS, "camera online")
    VerifiedEvent(candidate(), (ok,), 0.9, 10)
    bad = ValidatorVerdict("scene_stability", ValidatorOutcome.REJECT, "lights changed")
    with pytest.raises(ValueError, match="REJECT"):
        VerifiedEvent(candidate(), (ok, bad), 0.9, 10)


def test_every_verdict_needs_a_reason() -> None:
    with pytest.raises(ValueError):
        ValidatorVerdict("v", ValidatorOutcome.PASS, "  ")


# -------- camera / source


def spec(locator: str = "webcam:0") -> SourceSpec:
    return SourceSpec(SourceKind.WEBCAM, locator, {"device_index": 0})


def test_source_spec_refuses_credentials_in_the_locator() -> None:
    spec("rtsp://10.0.0.5/stream")
    with pytest.raises(ValueError, match="credentials"):
        spec("rtsp://admin:hunter2@10.0.0.5/stream")


def test_source_spec_options_are_read_only() -> None:
    with pytest.raises(TypeError):
        spec().options["x"] = 1  # type: ignore[index]


def cam(**kw: object) -> Camera:
    base: dict[str, object] = dict(
        camera_id="cam-a",
        name="A",
        source=spec(),
        location=None,
        priority=5,
        enabled=True,
        zones_ref=None,
        target_inference_fps=5.0,
        tags=(),
    )
    return Camera(**{**base, **kw})  # type: ignore[arg-type]


def test_camera_validation() -> None:
    assert cam().priority == 5
    for bad in (
        {"priority": 10},
        {"priority": -1},
        {"target_inference_fps": 0},
        {"name": " "},
        {"camera_id": "Bad"},
    ):
        with pytest.raises(ValueError):
            cam(**bad)


def health(**kw: object) -> CameraHealth:
    base: dict[str, object] = dict(
        camera_id="cam-a",
        state=CameraState.ONLINE,
        since_wall_utc=T0,
        stream_epoch=1,
        reconnect_attempts=0,
        last_frame_wall_utc=None,
        measured_fps=None,
        frames_received=0,
        frames_skipped=0,
        frames_dropped=0,
        decode_errors=0,
        detail=None,
    )
    return CameraHealth(**{**base, **kw})  # type: ignore[arg-type]


def test_camera_health_distinguishes_skipped_from_dropped() -> None:
    h = health(frames_skipped=4, frames_dropped=1)
    assert (h.frames_skipped, h.frames_dropped) == (4, 1)
    with pytest.raises(ValueError):
        health(frames_dropped=-1)
    with pytest.raises(ValueError):
        health(measured_fps=-1.0)


# -------- evidence / alert / metric


def evidence(**kw: object) -> Evidence:
    base: dict[str, object] = dict(
        evidence_id=ULID,
        incident_id=ULID2,
        kind=EvidenceKind.SNAPSHOT_RAW,
        relative_path="cam-a/2026/01/01/x/snapshot_raw.jpg",
        sha256="a" * 64,
        size_bytes=10,
        media_type="image/jpeg",
        captured_wall_utc=T0,
        span_ms=None,
        expires_wall_utc=None,
    )
    return Evidence(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "C:\\x\\y.jpg", "../escape.jpg", "a/../../b.jpg", "\\\\host\\share\\f"]
)
def test_evidence_paths_must_be_relative_and_contained(path: str) -> None:
    with pytest.raises(ValueError):
        evidence(relative_path=path)


def test_evidence_integrity_fields() -> None:
    evidence()
    for bad in (
        {"sha256": "XYZ"},
        {"sha256": "A" * 64},
        {"size_bytes": -1},
        {"span_ms": (5.0, 1.0)},
        {"evidence_id": "nope"},
    ):
        with pytest.raises(ValueError):
            evidence(**bad)


def test_alert_delivery_rules() -> None:
    AlertDelivery("console", True, 1, None)
    with pytest.raises(ValueError):
        AlertDelivery("console", True, 1, "boom")
    Alert(ULID, ULID2, Severity.HIGH, T0, ("console",), ())


def test_severity_is_ordered() -> None:
    assert Severity.CRITICAL > Severity.HIGH > Severity.MODERATE > Severity.LOW > Severity.INFO


def test_system_metric_accepts_missing_gpu_but_not_nonsense() -> None:
    kw: dict[str, object] = dict(
        t_wall_utc=T0,
        cpu_percent=10.0,
        memory_used_mb=100.0,
        process_rss_mb=50.0,
        gpu_utilization_percent=None,
        gpu_memory_used_mb=None,
        gpu_temperature_c=None,
        pipeline_fps=None,
        inference_latency_p50_ms=None,
        inference_latency_p95_ms=None,
        queue_depths={"capture:cam-a": 1},
        frames_dropped_total=0,
    )
    m = SystemMetric(**kw)  # type: ignore[arg-type]
    assert m.gpu_utilization_percent is None  # unmeasured is None, never 0
    with pytest.raises(ValueError):
        SystemMetric(**{**kw, "cpu_percent": 101.0})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SystemMetric(**{**kw, "gpu_utilization_percent": 150.0})  # type: ignore[arg-type]


def test_explanation_requires_a_trigger_summary() -> None:
    with pytest.raises(ValueError):
        IncidentExplanation("", (), (), (), (), {}, ())
