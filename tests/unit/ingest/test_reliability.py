"""The P6 camera-reliability fixes, each pinned by the consequence it had rather than the line
of code it touched.

Every one of these was a defect that existed from P0 and that no test caught, because each is a
property of how two components behave *together* over time. They are kept in one file so the
reasoning stays in one place: `docs/adr/0015-p6-perception-and-hardening-decisions.md` §B lists
them in the same order.
"""

from __future__ import annotations

import threading

import pytest

from vigil.config.schema.pipeline import HealthConfig, IngestConfig
from vigil.core.clock import ManualClock
from vigil.core.errors import SourceError, TransientSourceError
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.core.rng import seeded_rng
from vigil.domain.enums import CameraState
from vigil.ingest.buffers import LatestFrameSlot
from vigil.ingest.capture_worker import CaptureWorker, StepKind
from vigil.ingest.health import CameraHealthTracker
from vigil.ingest.pixel_buffer import NumpyPixelBuffer
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry

CAMERA = "cam-a"


def numpy_frame(width: int = 64, height: int = 48) -> RawFrame:
    np = pytest.importorskip("numpy")
    return RawFrame(NumpyPixelBuffer(np.zeros((height, width, 3), dtype=np.uint8)))


def tracker(
    clock: ManualClock,
    *,
    ingest: IngestConfig | None = None,
    health: HealthConfig | None = None,
) -> CameraHealthTracker:
    return CameraHealthTracker(
        CAMERA,
        clock=clock,
        ingest=ingest or IngestConfig(),
        health=health or HealthConfig(),
    )


# ------------------------------------------------------------------ F1: the fps expectation
#
# The consequence this guards: DEGRADED is in `verification`'s UNHEALTHY_STATES, so it rejects
# *every* candidate (06 §6 validator 1). A camera held DEGRADED for its whole life detects
# nothing, and an optimistic driver was enough to cause that. The development webcam advertises
# 30 fps and delivers about 14.


def deliver(t: CameraHealthTracker, clock: ManualClock, *, fps: float, frames: int) -> None:
    step_ms = 1000.0 / fps
    for _ in range(frames):
        clock.advance_ms(step_ms)
        t.record_frame(clock.monotonic_ns(), clock.wall_utc())


def test_a_driver_that_overstates_its_rate_does_not_silence_the_camera() -> None:
    """The defect: 14 fps delivered against a claimed 30 is 0.47 of the claim, under the 0.5
    floor, so the camera was DEGRADED for ever -- for a stream whose spacing is perfectly
    regular and which the accumulator would have reasoned over quite happily."""
    clock = ManualClock()
    t = tracker(clock, health=HealthConfig(adopt_measured_fps_after_frames=30))
    t.mark_online(epoch=1, expected_fps=30.0)
    deliver(t, clock, fps=14.0, frames=20)
    t.evaluate(clock.monotonic_ns())
    # Before it has demonstrated anything it is held to the claim, which is the honest default.
    assert t.state is CameraState.DEGRADED
    assert "low fps" in (t.snapshot().detail or "")

    # Once it has sustained its real rate it is held to that instead, and recovers.
    deliver(t, clock, fps=14.0, frames=20)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.ONLINE
    detail = t.snapshot().detail or ""
    assert "recovered" in detail


def test_the_expectation_names_the_claim_it_replaced() -> None:
    """An operator has to be able to see that the driver was wrong, not just that fps is low."""
    clock = ManualClock()
    t = tracker(clock, health=HealthConfig(adopt_measured_fps_after_frames=30))
    t.mark_online(epoch=1, expected_fps=30.0)
    deliver(t, clock, fps=14.0, frames=40)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.ONLINE
    # Now it really does get worse: 4 fps against the 14 it demonstrated.
    deliver(t, clock, fps=4.0, frames=40)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.DEGRADED
    detail = t.snapshot().detail or ""
    assert "low fps" in detail
    assert "driver claimed 30.0" in detail


def test_a_camera_below_the_absolute_floor_is_degraded_however_steady_it_is() -> None:
    """Demonstration does not lower the floor: frames 2 s apart are too coarse to reason over
    whatever the camera has 'proved' it can manage."""
    clock = ManualClock()
    t = tracker(
        clock,
        ingest=IngestConfig(stall_timeout_ms=600_000),
        health=HealthConfig(adopt_measured_fps_after_frames=10, min_absolute_fps=1.0),
    )
    t.mark_online(epoch=1, expected_fps=0.5)
    deliver(t, clock, fps=0.5, frames=40)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.DEGRADED
    assert "absolute floor" in (t.snapshot().detail or "")


def test_the_expectation_is_never_raised_above_the_claim() -> None:
    """A camera that briefly beat its claim is not then held to the better number."""
    clock = ManualClock()
    t = tracker(clock, health=HealthConfig(adopt_measured_fps_after_frames=10))
    t.mark_online(epoch=1, expected_fps=10.0)
    deliver(t, clock, fps=30.0, frames=20)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.ONLINE
    # Back to its claimed 10 fps: that is what it promised, so it is not DEGRADED for it.
    deliver(t, clock, fps=10.0, frames=40)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.ONLINE


def test_the_expectation_is_adopted_once_per_stream_epoch() -> None:
    """Re-adopting continuously would track a dying camera all the way down and never report."""
    clock = ManualClock()
    t = tracker(clock, health=HealthConfig(adopt_measured_fps_after_frames=10))
    t.mark_online(epoch=1, expected_fps=30.0)
    deliver(t, clock, fps=30.0, frames=20)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.ONLINE
    deliver(t, clock, fps=5.0, frames=40)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.DEGRADED


# ------------------------------------------------------------------ F5: a mid-stream format change


def test_a_format_change_keeps_the_rate_the_driver_reported() -> None:
    """`mark_online(expected_fps=None)` on a resolution change used to erase the known rate,
    silently disabling the low-fps check for the rest of the run."""
    clock = ManualClock()
    t = tracker(clock, health=HealthConfig(adopt_measured_fps_after_frames=10_000))
    t.mark_online(epoch=1, expected_fps=30.0)
    t.mark_online(epoch=2, expected_fps=None)  # what a format change does
    deliver(t, clock, fps=5.0, frames=20)
    t.evaluate(clock.monotonic_ns())
    assert t.state is CameraState.DEGRADED
    assert "30.0 expected" in (t.snapshot().detail or "")


# ------------------------------------------------------------------ F7: dropped frames


def test_dropped_frames_are_counted_with_a_count() -> None:
    """A queue evicts several at once; one call per eviction, not per frame."""
    clock = ManualClock()
    t = tracker(clock)
    t.record_dropped(7)
    t.record_dropped()
    assert t.snapshot().frames_dropped == 8


# ------------------------------------------------------------------ F2: reconnect escalation


class FlappingSource:
    """Opens every time and fails its first read every time: the shape F2 could not escape."""

    def __init__(self, *, frames_before_failing: int = 0) -> None:
        self.frames_before_failing = frames_before_failing
        self.opens = 0
        self._open = False
        self._delivered = 0

    @property
    def spec(self) -> object:
        return None

    @property
    def timing(self) -> SourceTiming:
        return SourceTiming.LIVE

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> SourceInfo:
        self.opens += 1
        self._open = True
        self._delivered = 0
        return SourceInfo(width_px=64, height_px=48, fps=30.0, backend="fake", detail="fake")

    def read(self) -> RawFrame | None:
        if self._delivered < self.frames_before_failing:
            self._delivered += 1
            return numpy_frame()
        raise SourceError("the device went away", retryable=True)

    def close(self) -> None:
        self._open = False


def worker_for(
    source: FlappingSource, *, ingest: IngestConfig, clock: ManualClock
) -> tuple[CaptureWorker, CameraHealthTracker]:
    health = tracker(clock, ingest=ingest)
    worker = CaptureWorker(
        camera_id=CAMERA,
        source=source,  # type: ignore[arg-type]
        channel=LatestFrameSlot(threading.Event()),
        health=health,
        clock=clock,
        ingest=ingest,
        metrics=MetricsRegistry(),
        rng=seeded_rng(0, "test"),
        logger=get_logger("test.capture"),
    )
    return worker, health


def cycle(worker: CaptureWorker, stop: threading.Event, *, steps: int) -> list[float]:
    """Run `steps` steps and return every non-zero wait the worker asked for."""
    waits: list[float] = []
    for _ in range(steps):
        result = worker.step(stop)
        if result.wait_ms > 0:
            waits.append(result.wait_ms)
        if result.kind in (StepKind.FAILED, StepKind.END_OF_STREAM, StepKind.STOPPED):
            break
    return waits


def test_a_flapping_camera_escalates_its_backoff_instead_of_hammering_the_device() -> None:
    """The defect: `_read_failed` cleared the attempt counter and so did the next successful
    open, so a camera that opened and then failed its read retried at the 500 ms base delay for
    ever. Jitter is +/-20%, so the assertion is on the trend, not on exact values."""
    clock = ManualClock()
    ingest = IngestConfig(reconnect_base_ms=500, reconnect_max_ms=30_000, max_reconnect_attempts=0)
    source = FlappingSource()
    worker, _ = worker_for(source, ingest=ingest, clock=clock)
    waits = cycle(worker, threading.Event(), steps=12)
    assert len(waits) >= 5
    assert waits[0] < 700.0  # the first retry is prompt
    assert waits[4] > waits[0] * 4  # and the fifth is not


def test_a_flapping_camera_eventually_reaches_failed() -> None:
    """`max_reconnect_attempts` was unreachable on the read-failure path, so a dead camera
    retried for ever and the operator was never told it had given up."""
    clock = ManualClock()
    ingest = IngestConfig(max_reconnect_attempts=3)
    source = FlappingSource()
    worker, health = worker_for(source, ingest=ingest, clock=clock)
    stop = threading.Event()
    kinds: list[StepKind] = []
    # Stepping stops at a terminal result, as `run()` does: a worker that has given up does not
    # keep reopening the device.
    for _ in range(12):
        result = worker.step(stop)
        kinds.append(result.kind)
        if result.kind is StepKind.FAILED:
            break
    assert kinds[-1] is StepKind.FAILED
    assert health.state is CameraState.FAILED
    assert "gave up after" in (health.snapshot().detail or "")


def test_a_reconnect_that_actually_works_resets_the_escalation() -> None:
    """A camera that recovers and runs is not punished for an old outage: the next failure
    starts from the base delay again."""
    clock = ManualClock()
    ingest = IngestConfig(reconnect_reset_frames=3, max_reconnect_attempts=0)
    source = FlappingSource(frames_before_failing=5)
    worker, _ = worker_for(source, ingest=ingest, clock=clock)
    waits = cycle(worker, threading.Event(), steps=40)
    assert len(waits) >= 3
    # Every cycle delivered 5 frames, which is past `reconnect_reset_frames`, so no escalation.
    assert max(waits) < 700.0


def test_a_camera_that_never_delivers_enough_frames_still_escalates() -> None:
    """One frame per reconnect is flapping, not recovery."""
    clock = ManualClock()
    ingest = IngestConfig(reconnect_reset_frames=10, max_reconnect_attempts=0)
    source = FlappingSource(frames_before_failing=1)
    worker, _ = worker_for(source, ingest=ingest, clock=clock)
    waits = cycle(worker, threading.Event(), steps=30)
    assert len(waits) >= 5
    assert waits[4] > waits[0] * 4


def test_a_transient_read_glitch_is_not_a_reconnect() -> None:
    """Decode errors are counted and waited out; they must not escalate the reconnect delay."""

    class Glitchy(FlappingSource):
        def read(self) -> RawFrame | None:
            raise TransientSourceError("a torn frame")

    clock = ManualClock()
    ingest = IngestConfig(max_consecutive_read_failures=100, glitch_wait_ms=10)
    worker, health = worker_for(Glitchy(), ingest=ingest, clock=clock)
    waits = cycle(worker, threading.Event(), steps=20)
    assert waits and all(w == 10.0 for w in waits)
    assert health.snapshot().decode_errors > 0
