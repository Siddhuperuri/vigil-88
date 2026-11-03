"""The degradation ladder (01 §9, plan D2): shed load in a fixed order, say so, and come back.

    step 0  NORMAL         everything runs at its configured rate
    step 1  REDUCED        every camera's inference rate is scaled down
    step 2  PRIORITY_ONLY  rates are scaled down further and cameras below the top priority are
                           suspended: they keep capturing, they are not analysed, and they show
                           ANALYSIS_SUSPENDED with the reason

**It never hides itself.** The state, the reason and the time it began are published in the
snapshot, the health report, and the camera states. A camera that is not being analysed is shown
as such, because an operator who believes it is watching a camera that is not is the failure this
exists to prevent.

The decision (`Ladder`) is a pure state machine over "was there pressure in this interval", so it
is tested without threads, a clock, or a detector. `DegradationController` reads the real pressure
signals and applies a step to the scheduler and the camera trackers.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Protocol

from vigil.config.schema.degradation import DegradationConfig
from vigil.core.clock import Clock
from vigil.core.heartbeat import wait_beating
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.enums import CameraState
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.camera_runtime import CameraRuntime


class Step(IntEnum):
    NORMAL = 0
    REDUCED = 1
    PRIORITY_ONLY = 2


STEP_LABEL: Mapping[Step, str] = {
    Step.NORMAL: "normal",
    Step.REDUCED: "reduced rate",
    Step.PRIORITY_ONLY: "priority cameras only",
}


@dataclass(frozen=True, slots=True)
class DegradationState:
    step: int
    label: str
    reason: str | None
    since_wall_utc: datetime | None
    suspended: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.step > 0


NORMAL_STATE = DegradationState(0, STEP_LABEL[Step.NORMAL], None, None, ())


@dataclass(frozen=True, slots=True)
class Pressure:
    """What one interval looked like."""

    pressured: bool
    reason: str


class Ladder:
    """Counts consecutive pressured or calm intervals and moves one step at a time."""

    def __init__(self, config: DegradationConfig) -> None:
        self._escalate_after = max(1, round(config.escalate_after_ms / config.interval_ms))
        self._recover_after = max(1, round(config.recover_after_ms / config.interval_ms))
        self.step = Step.NORMAL
        self._pressured_run = 0
        self._calm_run = 0

    def observe(self, sample: Pressure) -> Step | None:
        """Feed one interval. Returns the new step if it moved, else None."""
        if sample.pressured:
            self._pressured_run += 1
            self._calm_run = 0
            if self._pressured_run >= self._escalate_after and self.step < Step.PRIORITY_ONLY:
                self._pressured_run = 0
                self.step = Step(self.step + 1)
                return self.step
        else:
            self._calm_run += 1
            self._pressured_run = 0
            if self._calm_run >= self._recover_after and self.step > Step.NORMAL:
                self._calm_run = 0
                self.step = Step(self.step - 1)
                return self.step
        return None


class RateControl(Protocol):
    """The slice of the scheduler the ladder drives."""

    def set_rate_scale(self, scale: float) -> None: ...
    def set_suspended(self, camera_ids: frozenset[str]) -> None: ...


class DegradationController:
    def __init__(
        self,
        *,
        config: DegradationConfig,
        scheduler: RateControl,
        cameras: Callable[[], Mapping[str, CameraRuntime]],
        metrics: MetricsRegistry,
        clock: Clock,
        logger: LoggerLike,
    ) -> None:
        self._config = config
        self._scheduler = scheduler
        self._cameras = cameras
        self._metrics = metrics
        self._clock = clock
        self._log = logger.bind(stage="degradation")
        self._ladder = Ladder(config)
        self._lock = threading.Lock()
        self._state = NORMAL_STATE
        self._last_dropped = metrics.counter_total("vigil_frames_analysis_dropped_total")

    @property
    def state(self) -> DegradationState:
        with self._lock:
            return self._state

    # ---------------------------------------------------------------- sensing

    def sense(self) -> Pressure:
        dropped_total = self._metrics.counter_total("vigil_frames_analysis_dropped_total")
        dropped = int(dropped_total - self._last_dropped)
        self._last_dropped = dropped_total
        if dropped >= self._config.drop_threshold:
            return Pressure(True, f"analysis dropped {dropped} frames in one interval")
        latency = self._metrics.histogram("vigil_inference_latency_ms").summary()
        if latency.p95 is not None and latency.p95 > self._config.latency_ceiling_ms:
            ceiling = self._config.latency_ceiling_ms
            return Pressure(
                True, f"detector p95 latency {latency.p95:.0f} ms is above {ceiling:.0f} ms"
            )
        return Pressure(False, "no pressure")

    # ---------------------------------------------------------------- acting

    def tick(self) -> Step | None:
        """One interval: sense, decide, apply. Returns the new step if it changed."""
        sample = self.sense()
        moved = self._ladder.observe(sample)
        if moved is not None:
            self._apply(moved, sample.reason)
        else:
            self._keep_suspended_suspended()
        return moved

    def _keep_suspended_suspended(self) -> None:
        """A camera that reconnects while it is suspended comes back ONLINE, but the scheduler still
        is not serving it. Show what is true: it is not being analysed."""
        for cid in self.state.suspended:
            runtime = self._cameras().get(cid)
            if runtime is not None and runtime.health.state in (
                CameraState.ONLINE,
                CameraState.DEGRADED,
            ):
                runtime.health.transition(
                    CameraState.ANALYSIS_SUSPENDED,
                    "degradation step 2: still not analysed after reconnecting",
                )

    def _apply(self, step: Step, reason: str) -> None:
        cfg = self._config
        scale = {
            Step.NORMAL: 1.0,
            Step.REDUCED: cfg.reduced_rate_scale,
            Step.PRIORITY_ONLY: cfg.priority_only_rate_scale,
        }[step]
        self._scheduler.set_rate_scale(scale)
        suspended = self._suspend_below_top(step)
        self._scheduler.set_suspended(suspended)
        self._release_others(suspended)
        why = reason if step > Step.NORMAL else "pressure has cleared"
        with self._lock:
            self._state = (
                NORMAL_STATE
                if step is Step.NORMAL
                else DegradationState(
                    int(step),
                    STEP_LABEL[step],
                    why,
                    self._clock.wall_utc(),
                    tuple(sorted(suspended)),
                )
            )
        self._metrics.gauge("vigil_degradation_step").set(float(int(step)))
        self._log.warning(
            "degradation_step_changed", step=int(step), label=STEP_LABEL[step], reason=why
        )

    def _suspend_below_top(self, step: Step) -> frozenset[str]:
        if step is not Step.PRIORITY_ONLY:
            return frozenset()
        live = {cid: rt for cid, rt in self._cameras().items() if rt.captures and not rt.lossless}
        if not live:
            return frozenset()
        top = max(rt.camera.priority for rt in live.values())
        chosen: set[str] = set()
        for cid, rt in live.items():
            if rt.camera.priority < top and rt.health.state in (
                CameraState.ONLINE,
                CameraState.DEGRADED,
            ):
                rt.health.transition(
                    CameraState.ANALYSIS_SUSPENDED,
                    "degradation step 2: lower-priority cameras are not analysed while the "
                    "engine cannot keep up",
                )
                chosen.add(cid)
        return frozenset(chosen)

    def _release_others(self, still_suspended: frozenset[str]) -> None:
        for cid, rt in self._cameras().items():
            if cid not in still_suspended and rt.health.state is CameraState.ANALYSIS_SUSPENDED:
                rt.health.transition(CameraState.ONLINE, "degradation cleared")

    # ---------------------------------------------------------------- the thread

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        interval_s = self._config.interval_ms / 1000.0
        while not stop.is_set():
            beat()
            self.tick()
            wait_beating(stop, beat, interval_s)
