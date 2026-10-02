"""CaptureWorker behaviour, driven step by step: no threads, no sleeping, a manual clock."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from tests.support.fakes import BlockUntil, ScriptedSource, raw_frame
from vigil.config.schema.pipeline import HealthConfig, IngestConfig
from vigil.core.clock import ManualClock
from vigil.core.errors import SourceError, TransientSourceError
from vigil.core.protocols.source import SourceTiming
from vigil.core.rng import seeded_rng
from vigil.domain import CameraState
from vigil.ingest.buffers import BlockingFrameQueue, FrameChannel, LatestFrameSlot
from vigil.ingest.capture_worker import CaptureWorker, StepKind
from vigil.ingest.health import CameraHealthTracker
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry

S = CameraState


class Rig:
    def __init__(
        self,
        source: ScriptedSource,
        clock: ManualClock,
        *,
        channel: FrameChannel | None = None,
        seek: object = None,
        **ingest: object,
    ) -> None:
        self.source = source
        self.clock = clock
        self.stop = threading.Event()
        self.metrics = MetricsRegistry()
        cfg = IngestConfig(**ingest)  # type: ignore[arg-type]
        self.health = CameraHealthTracker("cam-a", clock=clock, ingest=cfg, health=HealthConfig())
        self.channel: FrameChannel = channel or LatestFrameSlot()
        self.worker = CaptureWorker(
            camera_id="cam-a",
            source=source,
            channel=self.channel,
            health=self.health,
            clock=clock,
            ingest=cfg,
            metrics=self.metrics,
            rng=seeded_rng(0),
            logger=get_logger("test"),
            seek=seek,  # type: ignore[arg-type]
        )

    def step(self) -> StepKind:
        return self.worker.step(self.stop).kind

    def steps(self, n: int) -> list[StepKind]:
        return [self.step() for _ in range(n)]


def test_first_step_opens_and_marks_the_camera_online(clock: ManualClock) -> None:
    r = Rig(ScriptedSource([raw_frame()]), clock)
    assert r.step() is StepKind.OPENED
    snap = r.health.snapshot()
    assert snap.state is S.ONLINE and snap.stream_epoch == 1


def test_frames_carry_identity_and_capture_timestamps(clock: ManualClock) -> None:
    r = Rig(ScriptedSource([raw_frame(), raw_frame(), raw_frame()]), clock)
    r.step()
    metas = []
    for _ in range(3):
        clock.advance_ms(40)
        assert r.step() is StepKind.FRAME
        frame = r.channel.take(0)
        assert frame is not None
        metas.append(frame.meta)
    assert [m.frame_index for m in metas] == [0, 1, 2]
    assert {m.stream_epoch for m in metas} == {1}
    assert [m.t_monotonic_ns for m in metas] == [40_000_000, 80_000_000, 120_000_000]
    assert metas[2].wall_utc == clock.wall_utc()
    assert all(m.camera_id == "cam-a" and (m.width_px, m.height_px) == (64, 48) for m in metas)


def test_the_frame_is_stamped_immediately_after_read_returns(clock: ManualClock) -> None:
    """07 §4: the stamp reflects when read() returned, not when the loop began."""
    src = ScriptedSource([raw_frame()], on_read=lambda: clock.advance_ms(25))
    r = Rig(src, clock)
    r.step()
    r.step()
    frame = r.channel.take(0)
    assert frame is not None and frame.meta.t_monotonic_ns == 25_000_000


def test_end_of_stream_is_a_normal_terminal_event_not_an_error(clock: ManualClock) -> None:
    r = Rig(ScriptedSource([raw_frame()]), clock)
    assert r.steps(3) == [StepKind.OPENED, StepKind.FRAME, StepKind.END_OF_STREAM]
    assert r.health.state is S.OFFLINE and r.health.snapshot().detail == "end of stream"
    assert r.source.open_calls == 1  # no reconnect storm at EOF


def test_slot_overwrites_are_counted_as_skipped_not_dropped(clock: ManualClock) -> None:
    r = Rig(ScriptedSource([raw_frame() for _ in range(4)]), clock)
    r.step()
    r.steps(4)  # nobody consumes the slot
    snap = r.health.snapshot()
    assert (snap.frames_received, snap.frames_skipped, snap.frames_dropped) == (4, 3, 0)
    assert r.metrics.counter("vigil_frames_skipped_total", camera="cam-a").value == 3


def test_paced_sources_never_skip(clock: ManualClock) -> None:
    q = BlockingFrameQueue(8)
    r = Rig(ScriptedSource([raw_frame() for _ in range(5)], timing=SourceTiming.PACED), clock, channel=q)
    r.steps(6)
    assert q.depth() == 5 and r.health.snapshot().frames_skipped == 0


def test_a_blocked_paced_handoff_is_aborted_by_stop(clock: ManualClock) -> None:
    q = BlockingFrameQueue(1)
    r = Rig(ScriptedSource([raw_frame(), raw_frame()], timing=SourceTiming.PACED), clock, channel=q)
    r.steps(2)  # open + first frame fills the queue
    result: list[StepKind] = []
    t = threading.Thread(target=lambda: result.append(r.step()))
    t.start()
    r.stop.set()
    t.join(5)
    assert result == [StepKind.STOPPED]


def test_paced_timestamps_follow_the_media_pts_via_seek(clock: ManualClock) -> None:
    seeks: list[float] = []
    src = ScriptedSource([raw_frame(pts_ms=0.0), raw_frame(pts_ms=33.3)], timing=SourceTiming.PACED)
    r = Rig(src, clock, channel=BlockingFrameQueue(4), seek=seeks.append)
    r.steps(3)
    assert seeks == [0.0, 33.3]


# ------------------------------------------------------------------ open failures


def test_open_failure_backs_off_and_reports_reconnecting(clock: ManualClock) -> None:
    src = ScriptedSource([], open_results=[SourceError("device busy")])
    r = Rig(src, clock, max_initial_open_attempts=3)
    result = r.worker.step(r.stop)
    assert result.kind is StepKind.OPEN_FAILED and result.wait_ms > 0
    assert r.health.state is S.RECONNECTING
    assert "device busy" in (r.health.snapshot().detail or "")
    assert r.metrics.counter("vigil_source_open_failures_total", camera="cam-a").value == 1


def test_a_camera_that_never_came_online_fails_after_the_initial_attempt_limit(
    clock: ManualClock,
) -> None:
    src = ScriptedSource([], open_results=[SourceError("absent")] * 5)
    r = Rig(src, clock, max_initial_open_attempts=3)
    assert r.steps(3) == [StepKind.OPEN_FAILED, StepKind.OPEN_FAILED, StepKind.FAILED]
    assert r.health.state is S.FAILED
    assert "gave up after 3 attempts" in (r.health.snapshot().detail or "")


def test_non_retryable_open_failure_fails_immediately_without_retrying(clock: ManualClock) -> None:
    """Retrying bad credentials can lock an account (07 §10)."""
    src = ScriptedSource([], open_results=[SourceError("authentication failed", retryable=False)])
    r = Rig(src, clock)
    assert r.step() is StepKind.FAILED
    assert r.health.state is S.FAILED and src.open_calls == 1


def test_open_recovers_after_transient_failures(clock: ManualClock) -> None:
    src = ScriptedSource([raw_frame()], open_results=[SourceError("a"), SourceError("b")])
    r = Rig(src, clock, max_initial_open_attempts=5)
    assert r.steps(4) == [
        StepKind.OPEN_FAILED, StepKind.OPEN_FAILED, StepKind.OPENED, StepKind.FRAME,
    ]
    assert r.health.state is S.ONLINE and r.health.snapshot().reconnect_attempts == 0


def test_reconnect_limit_applies_only_after_the_camera_has_been_online(clock: ManualClock) -> None:
    src = ScriptedSource(
        [SourceError("lost")], open_results=[None, SourceError("x"), SourceError("y")]
    )
    r = Rig(src, clock, max_reconnect_attempts=2)
    assert r.steps(4) == [
        StepKind.OPENED, StepKind.READ_FAILED, StepKind.OPEN_FAILED, StepKind.FAILED,
    ]
    assert r.health.state is S.FAILED


def test_unlimited_reconnects_by_default_once_online(clock: ManualClock) -> None:
    src = ScriptedSource([SourceError("lost")], open_results=[None] + [SourceError("x")] * 20)
    r = Rig(src, clock)
    kinds = r.steps(15)
    assert StepKind.FAILED not in kinds and r.health.state is S.RECONNECTING


# ------------------------------------------------------------------ read failures


def test_read_failure_closes_the_source_reconnects_and_bumps_the_epoch(clock: ManualClock) -> None:
    src = ScriptedSource([raw_frame(), SourceError("usb unplugged"), raw_frame()])
    r = Rig(src, clock)
    assert r.steps(3) == [StepKind.OPENED, StepKind.FRAME, StepKind.READ_FAILED]
    assert r.health.state is S.RECONNECTING and src.close_calls == 1
    assert r.steps(2) == [StepKind.OPENED, StepKind.FRAME]
    assert r.health.state is S.ONLINE
    assert r.health.snapshot().stream_epoch == 2
    first = r.channel.take(0)
    assert first is not None
    assert (first.meta.stream_epoch, first.meta.frame_index) == (2, 0)  # index restarts per epoch


def test_non_retryable_read_failure_is_terminal(clock: ManualClock) -> None:
    src = ScriptedSource([SourceError("fatal", retryable=False)])
    r = Rig(src, clock)
    assert r.steps(2) == [StepKind.OPENED, StepKind.FAILED]
    assert r.health.state is S.FAILED


def test_isolated_glitches_are_tolerated_and_counted(clock: ManualClock) -> None:
    src = ScriptedSource(
        [TransientSourceError("bad"), TransientSourceError("bad"), raw_frame()]
    )
    r = Rig(src, clock, max_consecutive_read_failures=3)
    assert r.steps(4) == [StepKind.OPENED, StepKind.GLITCH, StepKind.GLITCH, StepKind.FRAME]
    snap = r.health.snapshot()
    assert snap.decode_errors == 2 and snap.state is S.ONLINE
    assert src.open_calls == 1  # no reconnect for a glitch


def test_a_run_of_glitches_escalates_to_a_reconnect(clock: ManualClock) -> None:
    src = ScriptedSource([TransientSourceError("bad")] * 10)
    r = Rig(src, clock, max_consecutive_read_failures=3)
    kinds = r.steps(5)
    assert kinds == [StepKind.OPENED, StepKind.GLITCH, StepKind.GLITCH, StepKind.GLITCH,
                     StepKind.READ_FAILED]
    assert r.health.state is S.RECONNECTING


def test_a_good_frame_resets_the_glitch_run(clock: ManualClock) -> None:
    script = [TransientSourceError("b"), TransientSourceError("b"), raw_frame()] * 3
    r = Rig(ScriptedSource(script), clock, max_consecutive_read_failures=2)
    kinds = r.steps(10)
    assert StepKind.READ_FAILED not in kinds


# ------------------------------------------------------------------ format change, stop, run


def test_a_resolution_change_mid_stream_starts_a_new_epoch(clock: ManualClock) -> None:
    src = ScriptedSource([raw_frame(64, 48), raw_frame(64, 48), raw_frame(128, 96)])
    r = Rig(src, clock)
    r.steps(4)
    metas = []
    while (f := r.channel.take(0)) is not None:
        metas.append(f.meta)
    last = metas[-1]
    assert (last.width_px, last.height_px) == (128, 96)
    assert (last.stream_epoch, last.frame_index) == (2, 0)


def test_step_after_stop_is_terminal_and_touches_nothing(clock: ManualClock) -> None:
    src = ScriptedSource([raw_frame()])
    r = Rig(src, clock)
    r.stop.set()
    assert r.step() is StepKind.STOPPED and src.open_calls == 0


def test_run_loops_to_end_of_stream_and_closes(clock: ManualClock) -> None:
    src = ScriptedSource([raw_frame() for _ in range(3)], timing=SourceTiming.PACED)
    r = Rig(src, clock, channel=BlockingFrameQueue(8))
    beats: list[int] = []
    r.worker.run(r.stop, lambda: beats.append(1))
    assert r.health.state is S.OFFLINE and src.close_calls >= 1 and beats
    assert r.health.snapshot().frames_received == 3


def test_run_stops_promptly_and_marks_the_camera_offline(clock: ManualClock) -> None:
    release = threading.Event()
    src = ScriptedSource([raw_frame(), BlockUntil(release)])
    r = Rig(src, clock)
    t = threading.Thread(target=lambda: r.worker.run(r.stop, lambda: None))
    t.start()
    for _ in range(200):
        if r.health.state is S.ONLINE:
            break
        threading.Event().wait(0.01)
    r.stop.set()
    release.set()
    t.join(5)
    assert not t.is_alive()
    assert r.health.state is S.OFFLINE and src.close_calls >= 1


def test_close_does_not_overwrite_a_failed_state(clock: ManualClock) -> None:
    r = Rig(ScriptedSource([], open_results=[SourceError("x", retryable=False)]), clock)
    r.step()
    r.worker.close()
    assert r.health.state is S.FAILED


def test_wall_clock_is_carried_alongside_monotonic(clock: ManualClock) -> None:
    r = Rig(ScriptedSource([raw_frame()]), clock)
    r.step()
    clock.advance_ms(1500)
    r.step()
    f = r.channel.take(0)
    assert f is not None
    assert f.meta.wall_utc - clock.wall_utc() == timedelta(0)
