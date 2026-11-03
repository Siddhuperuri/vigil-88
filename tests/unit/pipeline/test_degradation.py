"""The degradation ladder: it sheds load in order, says why, and comes back without flapping."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from vigil.config.schema.degradation import DegradationConfig
from vigil.core.clock import ManualClock
from vigil.domain.enums import CameraState
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.degradation import (
    NORMAL_STATE,
    DegradationController,
    Ladder,
    Pressure,
    Step,
)

HOT = Pressure(True, "hot")
CALM = Pressure(False, "calm")


def config(**change: Any) -> DegradationConfig:
    base = {"interval_ms": 1000, "escalate_after_ms": 3000, "recover_after_ms": 5000}
    return DegradationConfig(**{**base, **change})


# ------------------------------------------------------------------ the state machine


def test_pressure_must_last_before_a_step_is_taken() -> None:
    ladder = Ladder(config())
    assert [ladder.observe(HOT) for _ in range(2)] == [None, None]
    assert ladder.observe(HOT) is Step.REDUCED


def test_a_single_calm_interval_resets_the_count_of_pressure() -> None:
    ladder = Ladder(config())
    for _ in range(2):
        ladder.observe(HOT)
    ladder.observe(CALM)
    assert [ladder.observe(HOT) for _ in range(2)] == [None, None]  # started again from zero


def test_it_goes_down_one_step_at_a_time_and_stops_at_the_last() -> None:
    ladder = Ladder(config())
    seen = [ladder.observe(HOT) for _ in range(12)]
    assert [s for s in seen if s is not None] == [Step.REDUCED, Step.PRIORITY_ONLY]
    assert ladder.step is Step.PRIORITY_ONLY


def test_recovery_is_slower_than_escalation_and_also_one_step_at_a_time() -> None:
    ladder = Ladder(config())
    for _ in range(6):
        ladder.observe(HOT)
    assert ladder.step is Step.PRIORITY_ONLY
    moves = [ladder.observe(CALM) for _ in range(10)]
    assert [m for m in moves if m is not None] == [Step.REDUCED, Step.NORMAL]
    assert moves.index(Step.REDUCED) == 4  # the fifth calm interval


def test_alternating_pressure_never_moves_the_ladder() -> None:
    ladder = Ladder(config())
    for _ in range(50):
        assert ladder.observe(HOT) is None
        assert ladder.observe(CALM) is None
    assert ladder.step is Step.NORMAL


def test_calm_at_normal_stays_normal() -> None:
    ladder = Ladder(config())
    assert all(ladder.observe(CALM) is None for _ in range(20))


def test_recovery_may_not_be_faster_than_escalation() -> None:
    with pytest.raises(ValidationError, match="recover_after_ms"):
        DegradationConfig(escalate_after_ms=10_000, recover_after_ms=2000)


def test_the_second_step_may_not_be_gentler_than_the_first() -> None:
    with pytest.raises(ValidationError, match="priority_only_rate_scale"):
        DegradationConfig(reduced_rate_scale=0.3, priority_only_rate_scale=0.6)


# ------------------------------------------------------------------ the controller


@dataclass
class FakeScheduler:
    scale: float = 1.0
    suspended: frozenset[str] = frozenset()

    def set_rate_scale(self, scale: float) -> None:
        self.scale = scale

    def set_suspended(self, camera_ids: frozenset[str]) -> None:
        self.suspended = camera_ids


@dataclass
class FakeHealth:
    state: CameraState = CameraState.ONLINE
    moves: list[tuple[CameraState, str | None]] = field(default_factory=list)

    def transition(self, new: CameraState, detail: str | None = None) -> None:
        self.state = new
        self.moves.append((new, detail))


def runtime(priority: int, state: CameraState = CameraState.ONLINE, lossless: bool = False) -> Any:
    return SimpleNamespace(
        camera=SimpleNamespace(priority=priority),
        health=FakeHealth(state),
        captures=True,
        lossless=lossless,
    )


@dataclass
class Rig:
    controller: DegradationController
    scheduler: FakeScheduler
    metrics: MetricsRegistry
    cameras: dict[str, Any]


def rig(cameras: dict[str, Any] | None = None, **change: Any) -> Rig:
    metrics = MetricsRegistry()
    scheduler = FakeScheduler()
    fleet = cameras if cameras is not None else {}
    controller = DegradationController(
        config=config(**change),
        scheduler=scheduler,
        cameras=lambda: fleet,
        metrics=metrics,
        clock=ManualClock(),
        logger=get_logger("test.degradation"),
    )
    return Rig(controller, scheduler, metrics, fleet)


def drop(r: Rig, count: int) -> None:
    r.metrics.counter("vigil_frames_analysis_dropped_total", camera="a").inc(count)


def tick_hot(r: Rig, times: int) -> None:
    for _ in range(times):
        drop(r, 10)
        r.controller.tick()


def test_a_healthy_engine_never_leaves_normal() -> None:
    r = rig()
    for _ in range(30):
        r.controller.tick()
    assert r.controller.state == NORMAL_STATE and r.scheduler.scale == 1.0


def test_drops_below_the_threshold_are_not_pressure() -> None:
    r = rig(drop_threshold=5)
    for _ in range(10):
        drop(r, 4)
        r.controller.tick()
    assert r.controller.state.step == 0


def test_drops_step_the_rate_down_and_the_state_says_why() -> None:
    r = rig()
    tick_hot(r, 3)
    state = r.controller.state
    assert state.step == 1 and state.label == "reduced rate"
    assert state.reason is not None and "dropped 10 frames" in state.reason
    assert state.since_wall_utc is not None
    assert r.scheduler.scale == 0.5 and r.scheduler.suspended == frozenset()
    assert r.metrics.gauge("vigil_degradation_step").value == 1.0


def test_slow_inference_is_pressure_too() -> None:
    r = rig(latency_ceiling_ms=100.0)
    for _ in range(20):
        r.metrics.histogram("vigil_inference_latency_ms").observe(400.0)
    for _ in range(3):
        r.controller.tick()
    assert r.controller.state.step == 1
    assert "p95 latency 400 ms" in (r.controller.state.reason or "")


def test_step_two_suspends_only_cameras_below_the_top_priority() -> None:
    fleet = {
        "top": runtime(9),
        "also-top": runtime(9),
        "low": runtime(2),
        "off": runtime(1, CameraState.OFFLINE),
    }
    r = rig(fleet)
    tick_hot(r, 6)
    assert r.controller.state.step == 2 and r.scheduler.scale == 0.25
    assert r.scheduler.suspended == frozenset({"low"})
    assert fleet["low"].health.state is CameraState.ANALYSIS_SUSPENDED
    assert "degradation step 2" in (fleet["low"].health.moves[0][1] or "")
    assert fleet["top"].health.state is CameraState.ONLINE
    assert fleet["off"].health.state is CameraState.OFFLINE  # not touched: it was not running
    assert r.controller.state.suspended == ("low",)


def test_when_every_camera_is_equal_no_camera_is_suspended() -> None:
    fleet = {"a": runtime(5), "b": runtime(5)}
    r = rig(fleet)
    tick_hot(r, 6)
    assert r.controller.state.step == 2 and r.scheduler.suspended == frozenset()
    assert all(c.health.state is CameraState.ONLINE for c in fleet.values())


def test_a_replayed_file_is_never_suspended() -> None:
    fleet = {"live": runtime(9), "replay": runtime(1, lossless=True)}
    r = rig(fleet)
    tick_hot(r, 6)
    assert r.scheduler.suspended == frozenset()


def test_recovery_restores_the_rate_and_resumes_suspended_cameras() -> None:
    fleet = {"top": runtime(9), "low": runtime(2)}
    r = rig(fleet)
    tick_hot(r, 6)
    for _ in range(5):
        r.controller.tick()
    assert r.controller.state.step == 1
    assert fleet["low"].health.state is CameraState.ONLINE  # back in service at step 1
    assert fleet["low"].health.moves[-1][1] == "degradation cleared"
    for _ in range(5):
        r.controller.tick()
    assert r.controller.state == NORMAL_STATE and r.scheduler.scale == 1.0
    assert r.metrics.gauge("vigil_degradation_step").value == 0.0


def test_a_camera_that_dropped_offline_meanwhile_is_not_resurrected() -> None:
    fleet = {"top": runtime(9), "low": runtime(2)}
    r = rig(fleet)
    tick_hot(r, 6)
    fleet["low"].health.state = CameraState.RECONNECTING  # the camera itself failed
    for _ in range(10):
        r.controller.tick()
    assert fleet["low"].health.state is CameraState.RECONNECTING


def test_no_live_camera_means_nothing_to_suspend() -> None:
    r = rig({})
    tick_hot(r, 6)
    assert r.controller.state.step == 2 and r.scheduler.suspended == frozenset()


def test_the_thread_loop_beats_ticks_and_stops() -> None:
    r = rig(interval_ms=100)
    stop = threading.Event()
    beats: list[int] = []

    def beat() -> None:
        beats.append(1)
        if len(beats) >= 3:
            stop.set()

    r.controller.run(stop, beat)
    assert len(beats) >= 3
