"""The tracker's claims: one identity across jitter and occlusion, and honest lifecycle state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise

import pytest

from vigil.config.schema.modules import TrackingConfig
from vigil.core.geometry import BBox
from vigil.domain.detection import Detection, DetectionSet, ModelDescriptor
from vigil.domain.enums import ObjectClass, TrackFlag
from vigil.domain.frame import FrameMeta
from vigil.domain.track import TrackedObject
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.tracking.assignment import greedy_match
from vigil.tracking.bytetrack import ByteTracker
from vigil.tracking.trajectory import fit_velocity_px_s, speed_box_heights_s

CAM = "test-cam"
WIDTH, HEIGHT = 640, 480
MODEL = ModelDescriptor("mock", "1.0", "mock", None, None, "fp32", "cpu", None)
WALL = datetime(2026, 1, 1, tzinfo=UTC)


def meta(index: int, t_ms: float, *, stream_epoch: int = 1) -> FrameMeta:
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


def detection(
    box: BBox, *, confidence: float = 0.9, object_class: ObjectClass = ObjectClass.PERSON
) -> Detection:
    return Detection(box, object_class, confidence, object_class.value)


def person_at(x: float, *, confidence: float = 0.9) -> Detection:
    return detection(BBox(x, 200.0, x + 50.0, 400.0), confidence=confidence)


@pytest.fixture
def tracker() -> ByteTracker:
    return ByteTracker(
        camera_id=CAM,
        config=TrackingConfig(),
        logger=get_logger("test.track"),
        metrics=MetricsRegistry(),
    )


def feed(
    tracker: ByteTracker,
    detections: Sequence[Detection],
    index: int,
    t_ms: float,
    *,
    stream_epoch: int = 1,
) -> Sequence[TrackedObject]:
    frame_meta = meta(index, t_ms, stream_epoch=stream_epoch)
    result = DetectionSet(frame_meta, tuple(detections), MODEL, 1.0)
    return tracker.update(result, frame_meta)


# ---------------------------------------------------------------- assignment


def test_greedy_match_takes_the_strongest_pair_first() -> None:
    # Row 0 slightly prefers column 1, but row 1 needs column 1 much more. Greedy keeps the
    # confident pair rather than trading it away, which is the behaviour we want.
    similarity = [[0.5, 0.6], [0.1, 0.95]]
    result = greedy_match(similarity, min_similarity=0.3, rows=2, columns=2)
    assert result.matches == ((1, 1), (0, 0))
    assert result.unmatched_rows == ()
    assert result.unmatched_columns == ()


def test_greedy_match_refuses_pairs_below_the_threshold() -> None:
    result = greedy_match([[0.2, 0.1]], min_similarity=0.3, rows=1, columns=2)
    assert result.matches == ()
    assert result.unmatched_rows == (0,)
    assert result.unmatched_columns == (0, 1)


def test_greedy_match_reports_empty_dimensions() -> None:
    assert greedy_match([], min_similarity=0.3, rows=0, columns=3).unmatched_columns == (0, 1, 2)


def test_greedy_match_breaks_ties_deterministically() -> None:
    similarity = [[0.5, 0.5], [0.5, 0.5]]
    first = greedy_match(similarity, min_similarity=0.1, rows=2, columns=2)
    second = greedy_match(similarity, min_similarity=0.1, rows=2, columns=2)
    assert first.matches == second.matches == ((0, 0), (1, 1))


# ---------------------------------------------------------------- identity


def test_one_identity_across_two_hundred_jittered_frames(tracker: ByteTracker) -> None:
    rng_offsets = [((i * 37) % 7) - 3 for i in range(200)]  # deterministic pseudo-jitter
    seen: set[int] = set()
    for i, jitter in enumerate(rng_offsets):
        tracks = feed(tracker, [person_at(100.0 + i * 1.5 + jitter)], i, i * 100.0)
        seen.update(t.track_id for t in tracks)
    assert seen == {1}


def test_identity_survives_two_brief_occlusions(tracker: ByteTracker) -> None:
    index = 0
    for step in range(10):
        feed(tracker, [person_at(100.0 + step * 5.0)], index, index * 100.0)
        index += 1
    for _ in range(2):  # occlusion 1: two missed updates
        feed(tracker, [], index, index * 100.0)
        index += 1
    tracks = feed(tracker, [person_at(100.0 + 12 * 5.0)], index, index * 100.0)
    index += 1
    assert [t.track_id for t in tracks] == [1]
    assert TrackFlag.REACQUIRED in tracks[0].flags

    for _ in range(3):  # occlusion 2
        feed(tracker, [], index, index * 100.0)
        index += 1
    tracks = feed(tracker, [person_at(100.0 + 16 * 5.0)], index, index * 100.0)
    assert [t.track_id for t in tracks] == [1]


def test_occluded_track_is_predicted_and_says_so(tracker: ByteTracker) -> None:
    for i in range(5):
        feed(tracker, [person_at(100.0 + i * 10.0)], i, i * 100.0)
    tracks = feed(tracker, [], 5, 500.0)
    assert len(tracks) == 1
    track = tracks[0]
    assert TrackFlag.OCCLUDED in track.flags
    assert track.time_since_update_frames == 1
    assert not track.is_visible
    assert track.trajectory.points[-1].is_interpolated


def test_a_low_confidence_detection_keeps_a_track_alive_but_starts_none(
    tracker: ByteTracker,
) -> None:
    for i in range(5):
        feed(tracker, [person_at(100.0 + i * 5.0)], i, i * 100.0)
    tracks = feed(tracker, [person_at(125.0, confidence=0.2)], 5, 500.0)
    assert [t.track_id for t in tracks] == [1]
    assert tracks[0].is_visible

    # The same weak detection somewhere new creates nothing.
    before = {t.track_id for t in tracks}
    tracks = feed(
        tracker,
        [person_at(130.0, confidence=0.2), person_at(500.0, confidence=0.2)],
        6,
        600.0,
    )
    assert {t.track_id for t in tracks} == before


def test_a_tentative_track_dies_faster_than_a_confirmed_one(tracker: ByteTracker) -> None:
    feed(tracker, [person_at(100.0)], 0, 0.0)  # one hit: tentative
    assert tracker.live_count == 1
    feed(tracker, [], 1, 100.0)
    feed(tracker, [], 2, 200.0)
    assert tracker.live_count == 0


def test_hits_never_exceed_age_and_the_hit_ratio_degrades_through_occlusion(
    tracker: ByteTracker,
) -> None:
    for i in range(6):
        feed(tracker, [person_at(100.0 + i * 5.0)], i, i * 100.0)
    tracks = feed(tracker, [], 6, 600.0)
    track = tracks[0]
    assert track.hits <= track.age_frames
    assert track.hit_ratio == pytest.approx(6 / 7)


def test_class_votes_resist_a_single_misclassification(tracker: ByteTracker) -> None:
    for i in range(6):
        feed(tracker, [person_at(100.0 + i * 2.0)], i, i * 100.0)
    box = BBox(112.0, 200.0, 162.0, 400.0)
    tracks = feed(tracker, [detection(box, object_class=ObjectClass.ANIMAL)], 6, 600.0)
    assert tracks[0].object_class is ObjectClass.PERSON


def test_two_people_keep_separate_identities(tracker: ByteTracker) -> None:
    for i in range(8):
        tracks = feed(
            tracker,
            [person_at(60.0 + i * 4.0), person_at(420.0 - i * 4.0)],
            i,
            i * 100.0,
        )
    assert sorted(t.track_id for t in tracks) == [1, 2]


# ---------------------------------------------------------------- motion


def test_velocity_matches_synthetic_ground_truth_within_five_percent(
    tracker: ByteTracker,
) -> None:
    for i in range(12):  # 8 px per 100 ms == 80 px/s
        tracks = feed(tracker, [person_at(100.0 + i * 8.0)], i, i * 100.0)
    velocity = tracks[0].velocity_px_s
    assert velocity is not None
    assert velocity.x == pytest.approx(80.0, rel=0.05)
    assert velocity.y == pytest.approx(0.0, abs=1e-6)


def test_velocity_is_none_until_enough_measured_history(tracker: ByteTracker) -> None:
    tracks = feed(tracker, [person_at(100.0)], 0, 0.0)
    assert tracks[0].velocity_px_s is None


def test_velocity_fit_ignores_interpolated_points() -> None:
    from vigil.domain.track import TrackPoint

    measured = [
        TrackPoint(int(i * 1e8), BBox(i * 10.0, 0.0, i * 10.0 + 10.0, 20.0), 0.9, False)
        for i in range(5)
    ]
    predicted = [
        TrackPoint(int((5 + i) * 1e8), BBox(999.0, 0.0, 1009.0, 20.0), 0.9, True) for i in range(3)
    ]
    velocity = fit_velocity_px_s([*measured, *predicted], window_frames=10, min_points=5)
    assert velocity is not None
    assert velocity.x == pytest.approx(100.0, rel=1e-6)


def test_speed_is_relative_to_the_box() -> None:
    from vigil.core.geometry import Vector2

    assert speed_box_heights_s(Vector2(100.0, 0.0), 200.0) == pytest.approx(0.5)
    assert speed_box_heights_s(None, 200.0) is None


def test_a_still_object_becomes_stationary_after_the_configured_dwell(
    tracker: ByteTracker,
) -> None:
    box = BBox(100.0, 100.0, 150.0, 300.0)
    for i in range(30):
        tracks = feed(tracker, [detection(box, object_class=ObjectClass.OBJECT_BAG)], i, i * 100.0)
    track = tracks[0]
    assert TrackFlag.STATIONARY in track.flags
    assert track.stationary_duration_ms is not None
    assert track.stationary_duration_ms >= TrackingConfig().stationary_min_duration_ms


def test_a_moving_object_is_not_stationary(tracker: ByteTracker) -> None:
    for i in range(30):
        tracks = feed(tracker, [person_at(50.0 + i * 15.0)], i, i * 100.0)
    assert TrackFlag.STATIONARY not in tracks[0].flags
    assert tracks[0].stationary_since_monotonic_ns is None


# ---------------------------------------------------------------- epochs and bounds


def test_a_stream_epoch_change_clears_every_identity(tracker: ByteTracker) -> None:
    for i in range(5):
        feed(tracker, [person_at(100.0 + i * 5.0)], i, i * 100.0)
    before = tracker.epoch
    tracks = feed(tracker, [person_at(125.0)], 0, 500.0, stream_epoch=2)
    assert tracker.epoch == before + 1
    assert [t.track_id for t in tracks] == [2]  # a new identity, not the old one reused
    assert tracks[0].tracker_epoch == tracker.epoch


def test_a_long_gap_resets_the_tracker(tracker: ByteTracker) -> None:
    for i in range(5):
        feed(tracker, [person_at(100.0 + i * 5.0)], i, i * 100.0)
    gap_ms = TrackingConfig().reset_gap_ms + 1000.0
    tracks = feed(tracker, [person_at(125.0)], 5, 400.0 + gap_ms)
    assert tracker.epoch == 2
    assert [t.track_id for t in tracks] == [2]


def test_reset_never_reuses_a_track_id(tracker: ByteTracker) -> None:
    feed(tracker, [person_at(100.0)], 0, 0.0)
    tracker.reset()
    tracks = feed(tracker, [person_at(100.0)], 1, 100.0)
    assert tracks[0].track_id == 2


def test_degenerate_and_surplus_detections_are_dropped_and_counted() -> None:
    metrics = MetricsRegistry()
    tracker = ByteTracker(
        camera_id=CAM,
        config=TrackingConfig(max_detections_per_frame=2),
        logger=get_logger("test.track"),
        metrics=metrics,
    )
    detections = [
        detection(BBox(10.0, 10.0, 10.5, 10.5)),  # degenerate
        detection(BBox(20.0, 20.0, 40.0, 60.0), confidence=0.05),  # below the low band
        person_at(100.0),
        person_at(200.0),
        person_at(300.0),
    ]
    tracker.update(DetectionSet(meta(0, 0.0), tuple(detections), MODEL, 1.0), meta(0, 0.0))
    assert metrics.counter("vigil_detections_ignored_total", camera=CAM).value == 3
    assert tracker.live_count == 2


def test_live_tracks_stay_inside_max_tracks() -> None:
    metrics = MetricsRegistry()
    tracker = ByteTracker(
        camera_id=CAM,
        config=TrackingConfig(max_tracks=3),
        logger=get_logger("test.track"),
        metrics=metrics,
    )
    for i in range(10):
        feed(tracker, [person_at(10.0 + j * 60.0) for j in range(6)], i, i * 100.0)
        assert tracker.live_count <= 3
    assert metrics.counter("vigil_tracks_evicted_total", camera=CAM).value > 0


def test_boxes_are_clipped_to_the_frame(tracker: ByteTracker) -> None:
    tracks = feed(tracker, [detection(BBox(-40.0, -20.0, 60.0, 300.0))], 0, 0.0)
    box = tracks[0].bbox
    assert box.x1 >= 0.0
    assert box.y1 >= 0.0
    assert box.x2 <= WIDTH
    assert box.y2 <= HEIGHT


def test_an_out_of_order_frame_does_not_corrupt_the_trajectory(tracker: ByteTracker) -> None:
    for i in range(4):
        feed(tracker, [person_at(100.0 + i * 5.0)], i, i * 100.0)
    tracks = feed(tracker, [person_at(118.0)], 4, 150.0)  # earlier than the last update
    points = tracks[0].trajectory.points
    assert all(a.t_monotonic_ns <= b.t_monotonic_ns for a, b in pairwise(points))
