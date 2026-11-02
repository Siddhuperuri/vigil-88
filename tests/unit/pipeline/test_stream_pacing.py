"""F8 (D0): the stream delivered 8.1 to 8.9 fps against a configured 10 because the worker tick
aliased with the camera analysis cadence. Pacing is now a slot grid, polled finely."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from vigil.core.clock import ManualClock
from vigil.core.protocols.transport import StreamKey
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline import streaming
from vigil.pipeline.streaming import POLL_S, StreamHub, StreamWorker

KEY = StreamKey("cam", "boxes")


class Results:
    """A stand-in for `LatestDetections`: a version counter and one analysed frame."""

    def __init__(self, clock: ManualClock) -> None:
        self._clock = clock
        self._version = 0

    def new_frame(self) -> None:
        self._version += 1

    def version(self, _camera_id: str) -> int:
        return self._version

    def latest(self, _camera_id: str) -> object:
        now = self._clock.monotonic_ns()
        meta = SimpleNamespace(source_pts_ms=None, t_monotonic_ns=now)
        return SimpleNamespace(completed_monotonic_ns=now, frame=SimpleNamespace(meta=meta))


@pytest.fixture(autouse=True)
def _no_real_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streaming, "render_jpeg", lambda *_a, **_k: b"jpeg")


def make_worker(clock: ManualClock, hub: StreamHub, results: Results, fps: float) -> StreamWorker:
    return StreamWorker(
        hub=hub,
        results=results,  # type: ignore[arg-type]
        clock=clock,
        metrics=MetricsRegistry(histogram_samples=64),
        logger=get_logger("t"),
        fps=fps,
        jpeg_quality=70,
    )


def run_stream(*, source_fps: float, cap_fps: float, seconds: float, jitter_ms: float = 0.0) -> int:
    """Drive the worker on a manual clock; return how many frames it published."""
    clock = ManualClock()
    hub = StreamHub()
    results = Results(clock)
    worker = make_worker(clock, hub, results, cap_fps)
    poll_ms = min(POLL_S, 1.0 / cap_fps / 4) * 1000
    interval_ms = 1000.0 / source_fps
    next_frame_ms, toggle = interval_ms, 1.0
    elapsed = 0.0
    with hub.subscribe(KEY):
        while elapsed < seconds * 1000:
            clock.advance_ms(poll_ms)
            elapsed += poll_ms
            while elapsed >= next_frame_ms:
                results.new_frame()
                toggle = -toggle  # alternate early and late: a camera is never perfectly even
                next_frame_ms += interval_ms + toggle * jitter_ms
            worker.step()
    latest = hub.latest(KEY)
    return 0 if latest is None else latest.seq


def test_a_ten_fps_camera_is_streamed_at_ten_fps_not_at_eight() -> None:
    published = run_stream(source_fps=10.0, cap_fps=10.0, seconds=20, jitter_ms=4.0)
    assert published >= 195, f"{published / 20:.2f} fps: the aliasing defect is back"
    assert published <= 201  # and never above the cap


def test_the_cap_is_never_exceeded_whatever_the_camera_rate() -> None:
    published = run_stream(source_fps=30.0, cap_fps=10.0, seconds=20)
    assert 195 <= published <= 201


def test_a_slower_camera_is_passed_through_untouched() -> None:
    assert run_stream(source_fps=5.0, cap_fps=10.0, seconds=20) == pytest.approx(100, abs=2)


def test_an_idle_stream_does_not_burst_when_frames_resume() -> None:
    clock = ManualClock()
    hub = StreamHub()
    results = Results(clock)
    worker = make_worker(clock, hub, results, 10.0)
    with hub.subscribe(KEY):
        clock.advance_ms(60_000)  # a minute of nothing
        for _ in range(5):  # then five frames in the same instant
            results.new_frame()
            worker.step()
        latest = hub.latest(KEY)
        assert latest is not None and latest.seq == 1  # one slot, not a backlog of five


def test_run_polls_faster_than_the_stream_rate_and_stops_promptly() -> None:
    clock = ManualClock()
    beats = 0

    def beat() -> None:
        nonlocal beats
        beats += 1

    stop = threading.Event()
    worker = make_worker(clock, StreamHub(), Results(clock), 10.0)
    thread = threading.Thread(target=worker.run, args=(stop, beat))
    thread.start()
    threading.Event().wait(0.25)
    stop.set()
    thread.join(2)
    assert not thread.is_alive() and beats >= 5  # a 10 fps tick would manage 2 or 3
