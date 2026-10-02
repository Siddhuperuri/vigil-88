from __future__ import annotations

import itertools

import pytest

from vigil.config.schema.pipeline import HealthConfig, IngestConfig
from vigil.core.bus import EventBus
from vigil.core.clock import ManualClock
from vigil.core.errors import LifecycleError
from vigil.core.units import ms_to_ns
from vigil.domain import CameraState, CameraStateChanged
from vigil.ingest.health import TRANSITIONS, CameraHealthTracker

S = CameraState


def tracker(
    clock: ManualClock, *, watch_stall: bool = True, bus: EventBus | None = None, **health: object
) -> CameraHealthTracker:
    return CameraHealthTracker(
        "cam-a",
        clock=clock,
        ingest=IngestConfig(),
        health=HealthConfig(**health),  # type: ignore[arg-type]
        bus=bus,
        watch_stall=watch_stall,
    )


def feed(t: CameraHealthTracker, clock: ManualClock, n: int, interval_ms: float) -> None:
    for _ in range(n):
        clock.advance_ms(interval_ms)
        t.record_frame(clock.monotonic_ns(), clock.wall_utc())


# ------------------------------------------------------------------ the transition table


@pytest.mark.parametrize(("old", "new"), list(itertools.product(S, S)))
def test_every_state_pair_follows_the_table(old: S, new: S, clock: ManualClock) -> None:
    t = CameraHealthTracker(
        "cam-a", clock=clock, ingest=IngestConfig(), health=HealthConfig(), initial_state=old
    )
    if new == old or new in TRANSITIONS[old]:
        t.transition(new, "x")
        assert t.state is new
    else:
        with pytest.raises(LifecycleError, match="illegal"):
            t.transition(new)
        assert t.state is old  # an illegal move changes nothing


def test_nothing_is_reachable_from_failed_or_offline_except_restart_or_disable() -> None:
    assert TRANSITIONS[S.FAILED] == {S.INITIALIZING, S.DISABLED}
    assert TRANSITIONS[S.OFFLINE] == {S.INITIALIZING, S.DISABLED}


def test_every_state_has_a_row() -> None:
    assert set(TRANSITIONS) == set(S)


# ------------------------------------------------------------------ transitions and events


def test_transitions_publish_events_and_same_state_does_not(clock: ManualClock) -> None:
    bus, events = EventBus(), []
    bus.subscribe(CameraStateChanged, events.append)
    t = tracker(clock, bus=bus)
    t.mark_online(epoch=1, expected_fps=30)
    t.mark_reconnecting("lost")
    t.mark_reconnecting("still lost")  # same state: detail update only
    assert [(e.old, e.new) for e in events] == [
        (S.INITIALIZING, S.ONLINE),
        (S.ONLINE, S.RECONNECTING),
    ]
    assert t.snapshot().detail == "still lost" and t.snapshot().reconnect_attempts == 2


def test_mark_online_resets_the_stream_state(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=30)
    feed(t, clock, 20, 33)
    t.mark_reconnecting("x")
    t.mark_online(epoch=2, expected_fps=30)
    snap = t.snapshot()
    assert snap.stream_epoch == 2 and snap.reconnect_attempts == 0 and snap.measured_fps is None
    assert snap.frames_received == 20  # lifetime counters survive a reconnect


def test_counters_and_snapshot(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=30)
    feed(t, clock, 3, 33)
    t.record_skipped()
    t.record_skipped()
    t.record_dropped()
    t.record_decode_error(clock.monotonic_ns())
    s = t.snapshot()
    assert (s.frames_received, s.frames_skipped, s.frames_dropped, s.decode_errors) == (3, 2, 1, 1)
    assert s.last_frame_wall_utc == clock.wall_utc()


def test_failed_offline_and_disabled_helpers(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_failed("no device")
    assert t.state is S.FAILED and t.snapshot().detail == "no device"
    t.mark_initializing()
    t.mark_offline("end of stream")
    assert t.state is S.OFFLINE
    t.mark_disabled()
    assert t.state is S.DISABLED


def test_analysis_suspended_is_reachable_and_recoverable(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=None)
    t.transition(S.ANALYSIS_SUSPENDED, "degradation step 4")
    t.transition(S.ONLINE)
    assert t.state is S.ONLINE


# ------------------------------------------------------------------ measurement


def test_fps_is_measured_from_frame_timestamps_only(clock: ManualClock) -> None:
    t = tracker(clock, min_frames_for_fps=10)
    t.mark_online(epoch=1, expected_fps=30)
    feed(t, clock, 9, 100)
    assert t.snapshot().measured_fps is None  # not enough samples to claim a number
    feed(t, clock, 11, 100)
    assert t.snapshot().measured_fps == pytest.approx(10.0)


# ------------------------------------------------------------------ evaluate: DEGRADED


def test_a_stalled_stream_becomes_degraded_then_recovers(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=30)
    feed(t, clock, 30, 33)
    t.evaluate()
    assert t.state is S.ONLINE
    clock.advance_ms(6000)  # no frames: longer than ingest.stall_timeout_ms (5000)
    t.evaluate()
    assert t.state is S.DEGRADED and "stalled" in (t.snapshot().detail or "")
    feed(t, clock, 40, 33)
    t.evaluate()
    assert t.state is S.ONLINE and t.snapshot().detail == "recovered"


def test_a_stream_that_never_delivers_a_frame_is_also_detected(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=30)
    clock.advance_ms(6000)
    t.evaluate()
    assert t.state is S.DEGRADED


def test_stall_threshold_adapts_to_a_slow_camera(clock: ManualClock) -> None:
    """A 0.1 fps camera (10 s between frames) is not stalled after 6 s."""
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=0.1)
    clock.advance_ms(6000)
    t.evaluate()
    assert t.state is S.ONLINE
    clock.advance_ms(30_000)  # > 3 x 10 s
    t.evaluate()
    assert t.state is S.DEGRADED


def test_low_fps_degrades(clock: ManualClock) -> None:
    t = tracker(clock, min_fps_ratio=0.5)
    t.mark_online(epoch=1, expected_fps=30)
    feed(t, clock, 40, 100)  # 10 fps measured vs 30 expected
    t.evaluate()
    assert t.state is S.DEGRADED and "low fps" in (t.snapshot().detail or "")


def test_decode_errors_degrade_and_age_out(clock: ManualClock) -> None:
    t = tracker(clock, max_decode_errors_per_window=3, decode_error_window_ms=10_000)
    t.mark_online(epoch=1, expected_fps=None)
    for _ in range(4):
        t.record_decode_error(clock.monotonic_ns())
    t.evaluate()
    assert t.state is S.DEGRADED and "decode errors" in (t.snapshot().detail or "")
    clock.advance_ms(11_000)
    t.record_frame(clock.monotonic_ns(), clock.wall_utc())
    t.evaluate()
    assert t.state is S.ONLINE


def test_paced_cameras_are_never_degraded_for_silence(clock: ManualClock) -> None:
    """A replayed file blocked by backpressure is quiet by design, not stalled."""
    t = tracker(clock, watch_stall=False)
    t.mark_online(epoch=1, expected_fps=30)
    clock.advance_ms(60_000)
    t.evaluate()
    assert t.state is S.ONLINE


def test_evaluate_ignores_cameras_that_are_not_streaming(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_reconnecting("x")
    clock.advance_ms(60_000)
    t.evaluate()
    assert t.state is S.RECONNECTING


def test_evaluate_does_not_clear_a_degraded_state_it_did_not_set(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=30)
    t.transition(S.DEGRADED, "set externally")
    feed(t, clock, 40, 33)
    t.evaluate()
    assert t.state is S.DEGRADED


def test_evaluate_accepts_an_explicit_now(clock: ManualClock) -> None:
    t = tracker(clock)
    t.mark_online(epoch=1, expected_fps=30)
    t.evaluate(clock.monotonic_ns() + ms_to_ns(10_000))
    assert t.state is S.DEGRADED
