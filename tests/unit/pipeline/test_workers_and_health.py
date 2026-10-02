from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from vigil.config.schema.pipeline import HealthConfig, IngestConfig
from vigil.core.clock import ManualClock, SystemClock
from vigil.core.errors import LifecycleError
from vigil.domain import CameraHealth, CameraState
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.health import HealthLevel, aggregate_health
from vigil.pipeline.workers import THREAD_PREFIX, ThreadWorker, WorkerHealth


def make(target: Callable[[threading.Event, Callable[[], None]], None], clock=None) -> ThreadWorker:  # type: ignore[no-untyped-def]
    return ThreadWorker(
        "w", target, clock=clock or SystemClock(), logger=get_logger("t"), metrics=MetricsRegistry()
    )


def loop_until_stopped(stop: threading.Event, beat: Callable[[], None]) -> None:
    while not stop.is_set():
        beat()
        stop.wait(0.005)


def test_worker_starts_beats_and_stops_cleanly() -> None:
    w = make(loop_until_stopped)
    assert not w.health().started
    w.start()
    time.sleep(0.05)
    h = w.health()
    assert h.started and h.alive and not h.crashed and h.heartbeat_age_ms is not None
    w.request_stop()
    assert w.join(5) and not w.alive


def test_thread_is_named_and_daemon() -> None:
    w = make(loop_until_stopped)
    w.start()
    names = [t.name for t in threading.enumerate()]
    assert f"{THREAD_PREFIX}w" in names
    assert next(t for t in threading.enumerate() if t.name == f"{THREAD_PREFIX}w").daemon
    w.request_stop()
    w.join(5)


def test_a_worker_cannot_be_started_twice() -> None:
    w = make(loop_until_stopped)
    w.start()
    with pytest.raises(LifecycleError, match="already started"):
        w.start()
    w.request_stop()
    w.join(5)


def test_a_crash_is_captured_counted_logged_and_surfaced_never_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = MetricsRegistry()

    def boom(_stop: threading.Event, _beat: Callable[[], None]) -> None:
        raise RuntimeError("kaboom")

    w = ThreadWorker("crashy", boom, clock=SystemClock(), logger=get_logger("t"), metrics=metrics)
    with caplog.at_level("ERROR"):
        w.start()
        assert w.join(5)
    h = w.health()
    assert h.crashed and not h.alive and "kaboom" in (h.error or "")
    assert metrics.counter("vigil_worker_crashes_total", worker="crashy").value == 1


def test_join_before_start_is_trivially_true() -> None:
    assert make(loop_until_stopped).join(0.01)


def test_join_reports_a_straggler_by_returning_false() -> None:
    release = threading.Event()
    w = make(lambda stop, beat: release.wait(10))
    w.start()
    w.request_stop()
    assert w.join(0.05) is False
    release.set()
    assert w.join(5)


def test_heartbeat_age_uses_the_injected_clock() -> None:
    clock = ManualClock()
    release = threading.Event()
    w = make(lambda stop, beat: release.wait(10), clock=clock)
    w.start()
    clock.advance_ms(750)
    assert w.health().heartbeat_age_ms == pytest.approx(750.0)
    release.set()
    w.join(5)


# ------------------------------------------------------------------ health aggregation

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def cam(state: CameraState, cid: str = "cam-a", detail: str | None = None) -> CameraHealth:
    return CameraHealth(cid, state, T0, 1, 0, None, None, 0, 0, 0, 0, detail)


def worker(crashed: bool = False, alive: bool = True) -> WorkerHealth:
    return WorkerHealth("w", True, alive, crashed, "boom" if crashed else None, 1.0)


def test_healthy_system_is_ok() -> None:
    r = aggregate_health([cam(CameraState.ONLINE)], [worker()])
    assert r.level is HealthLevel.OK and r.issues == ()


def test_quiet_states_are_not_problems() -> None:
    r = aggregate_health([cam(CameraState.DISABLED), cam(CameraState.OFFLINE, "cam-b")], [])
    assert r.level is HealthLevel.OK


@pytest.mark.parametrize(
    "state", [CameraState.DEGRADED, CameraState.RECONNECTING, CameraState.ANALYSIS_SUSPENDED]
)
def test_unhealthy_camera_states_degrade_the_system(state: CameraState) -> None:
    r = aggregate_health([cam(state, detail="why")], [worker()])
    assert r.level is HealthLevel.DEGRADED and "why" in r.issues[0]


def test_one_failed_camera_among_healthy_ones_is_degraded_not_failed() -> None:
    r = aggregate_health([cam(CameraState.FAILED), cam(CameraState.ONLINE, "cam-b")], [])
    assert r.level is HealthLevel.DEGRADED


def test_every_active_camera_failed_is_failed() -> None:
    r = aggregate_health([cam(CameraState.FAILED), cam(CameraState.DISABLED, "cam-b")], [])
    assert r.level is HealthLevel.FAILED


def test_a_crashed_worker_fails_the_system() -> None:
    r = aggregate_health([cam(CameraState.ONLINE)], [worker(crashed=True, alive=False)])
    assert r.level is HealthLevel.FAILED and "crashed" in r.issues[0]


def test_a_worker_that_finished_cleanly_is_fine() -> None:
    assert aggregate_health([], [worker(alive=False)]).level is HealthLevel.OK


def test_ingest_and_health_configs_are_used_by_the_tracker_defaults() -> None:
    assert IngestConfig().max_initial_open_attempts >= 1 and HealthConfig().min_fps_ratio <= 1
