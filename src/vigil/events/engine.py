"""Running a camera's modules over one scene (04 §1 `observe`, §3.5).

What this engine guarantees, which is what makes incident categories genuine plugins:

  * a module that raises does not reach the rest of the pipeline — it is quarantined after
    `modules.quarantine_threshold` consecutive errors and retried later (rule 5);
  * a module that overruns `modules.observe_budget_ms` is counted and, after three consecutive
    violations, suspended (rule 3);
  * a module whose declared capabilities this camera cannot supply never runs at all (D-008);
  * module state is reset when the tracker epoch changes, because identities no longer mean
    what they meant and stale state would attribute one object's history to another (rule 4).

The engine holds no temporal state. It returns observations; `pipeline/` feeds them to the
temporal engine and brings an accumulator state back for `promote`.

The injected `Clock` is used **only** to measure how long a module took. No decision in this
file reads it: decision time comes from `scene.frame_meta.t_monotonic_ns` (01 §7.6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from vigil.config.schema.modules import ModulesConfig
from vigil.core.clock import Clock
from vigil.core.errors import ModuleError, VigilError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.protocols.module import DetectionModule, ModuleContext, ModuleDescriptor
from vigil.core.units import ns_to_ms
from vigil.domain.observation import Observation
from vigil.domain.scene import SceneState
from vigil.events.registry import ModuleAvailability
from vigil.observability.metrics import MetricsRegistry

CONSECUTIVE_BUDGET_VIOLATIONS_TO_SUSPEND = 3


@dataclass(slots=True)
class ModuleRuntime:
    """One bound module and its health on one camera."""

    descriptor: ModuleDescriptor
    module: DetectionModule
    availability: ModuleAvailability
    # The resolved three-level merge (06 §5), not the descriptor defaults: a rebind on epoch
    # change must hand the module the same parameters it was started with.
    params: Mapping[str, object]
    consecutive_errors: int = 0
    consecutive_overruns: int = 0
    quarantined_until_ns: int | None = None
    suspended: bool = False
    observations_total: int = 0
    errors_total: int = 0
    overruns_total: int = 0
    last_error: str | None = None

    @property
    def module_id(self) -> str:
        return self.descriptor.module_id

    def is_runnable_at(self, now_ns: int) -> bool:
        if self.suspended:
            return False
        if self.quarantined_until_ns is None:
            return True
        return now_ns >= self.quarantined_until_ns

    @property
    def quarantined(self) -> bool:
        return self.quarantined_until_ns is not None


@dataclass(frozen=True, slots=True)
class ModuleFailure:
    module_id: str
    reason: str
    quarantined: bool
    suspended: bool


@dataclass(frozen=True, slots=True)
class ObserveResult:
    """What one analysis tick produced. Deterministic given the same scene and module state."""

    observations: tuple[Observation, ...] = ()
    failures: tuple[ModuleFailure, ...] = ()
    epoch_reset: bool = False
    skipped_module_ids: tuple[str, ...] = ()
    latency_ms_by_module: Mapping[str, float] = field(default_factory=dict)


class EventEngine:
    """Owns one camera's modules. Touched by exactly one thread (D-005), so it holds no lock."""

    __slots__ = (
        "_camera_id",
        "_clock",
        "_config",
        "_epoch",
        "_log",
        "_metrics",
        "_runtimes",
    )

    def __init__(
        self,
        *,
        camera_id: str,
        runtimes: Sequence[ModuleRuntime],
        config: ModulesConfig,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._camera_id = camera_id
        self._config = config
        self._clock = clock
        self._log = logger.bind(camera_id=camera_id, stage="observe")
        self._metrics = metrics
        self._runtimes = {r.module_id: r for r in runtimes}
        self._epoch: tuple[int, int] | None = None

    @property
    def camera_id(self) -> str:
        return self._camera_id

    def module_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._runtimes))

    def runtime(self, module_id: str) -> ModuleRuntime:
        try:
            return self._runtimes[module_id]
        except KeyError:
            raise ModuleError(
                f"module {module_id!r} is not bound on camera {self._camera_id!r}",
                context={"camera_id": self._camera_id, "module_id": module_id},
            ) from None

    def health(self) -> tuple[ModuleRuntime, ...]:
        return tuple(self._runtimes[mid] for mid in self.module_ids())

    # ---------------------------------------------------------------- the tick

    def observe(self, scene: SceneState) -> ObserveResult:
        """Run every runnable module over `scene` and collect their observations."""
        if scene.frame_meta.camera_id != self._camera_id:
            raise ModuleError(
                "scene belongs to a different camera",
                context={"expected": self._camera_id, "got": scene.frame_meta.camera_id},
            )
        now_ns = scene.frame_meta.t_monotonic_ns
        epoch_reset = self._apply_epoch(scene)

        observations: list[Observation] = []
        failures: list[ModuleFailure] = []
        skipped: list[str] = []
        latencies: dict[str, float] = {}

        for module_id in self.module_ids():
            runtime = self._runtimes[module_id]
            if not runtime.is_runnable_at(now_ns):
                skipped.append(module_id)
                continue
            if runtime.quarantined:
                self._leave_quarantine(runtime)
            emitted, failure, latency_ms = self._run_one(runtime, scene, now_ns)
            latencies[module_id] = latency_ms
            observations.extend(emitted)
            if failure is not None:
                failures.append(failure)

        return ObserveResult(
            observations=tuple(observations),
            failures=tuple(failures),
            epoch_reset=epoch_reset,
            skipped_module_ids=tuple(skipped),
            latency_ms_by_module=latencies,
        )

    def _run_one(
        self, runtime: ModuleRuntime, scene: SceneState, now_ns: int
    ) -> tuple[Sequence[Observation], ModuleFailure | None, float]:
        started_ns = self._clock.monotonic_ns()
        try:
            emitted = tuple(runtime.module.observe(scene))
        except VigilError as exc:
            return ((), self._record_error(runtime, exc.message, now_ns), 0.0)
        except Exception as exc:  # noqa: BLE001 - a plugin may raise anything; rule 5
            return ((), self._record_error(runtime, f"{type(exc).__name__}: {exc}", now_ns), 0.0)
        latency_ms = ns_to_ms(self._clock.monotonic_ns() - started_ns)

        invalid = self._validate_emitted(runtime, emitted, scene)
        if invalid is not None:
            return ((), self._record_error(runtime, invalid, now_ns), latency_ms)

        runtime.consecutive_errors = 0
        runtime.observations_total += len(emitted)
        self._metrics.histogram(
            "vigil_stage_latency_ms", stage="observe", camera=self._camera_id
        ).observe(latency_ms)
        self._metrics.counter(
            "vigil_observations_total", camera=self._camera_id, module=runtime.module_id
        ).inc(len(emitted))
        failure = self._check_budget(runtime, latency_ms)
        return (emitted, failure, latency_ms)

    def _validate_emitted(
        self, runtime: ModuleRuntime, emitted: Sequence[Observation], scene: SceneState
    ) -> str | None:
        """A module that mislabels its observations would poison another module's accumulator,
        so the engine checks rather than trusts. `signal` and timestamp ranges are already
        enforced by the `Observation` constructor."""
        for obs in emitted:
            if obs.module_id != runtime.module_id:
                return f"emitted an observation attributed to module {obs.module_id!r}"
            if obs.camera_id != self._camera_id:
                return f"emitted an observation attributed to camera {obs.camera_id!r}"
            if obs.t_monotonic_ns != scene.frame_meta.t_monotonic_ns:
                return (
                    "emitted an observation stamped with a time other than the frame's: a "
                    "module must not invent timestamps"
                )
        return None

    # ---------------------------------------------------------------- failure containment

    def _record_error(self, runtime: ModuleRuntime, reason: str, now_ns: int) -> ModuleFailure:
        runtime.consecutive_errors += 1
        runtime.errors_total += 1
        runtime.last_error = reason
        self._metrics.counter(
            "vigil_stage_errors_total", stage="observe", camera=self._camera_id
        ).inc()
        self._metrics.counter(
            "vigil_module_errors_total", camera=self._camera_id, module=runtime.module_id
        ).inc()
        quarantined = runtime.consecutive_errors >= self._config.quarantine_threshold
        if quarantined:
            runtime.quarantined_until_ns = now_ns + int(
                self._config.quarantine_retry_ms * 1_000_000
            )
            self._log.error(
                "module_quarantined",
                module_id=runtime.module_id,
                consecutive_errors=runtime.consecutive_errors,
                retry_in_ms=self._config.quarantine_retry_ms,
                reason=reason,
            )
        else:
            self._log.warning(
                "module_error",
                module_id=runtime.module_id,
                consecutive_errors=runtime.consecutive_errors,
                reason=reason,
            )
        return ModuleFailure(
            module_id=runtime.module_id,
            reason=reason,
            quarantined=quarantined,
            suspended=runtime.suspended,
        )

    def _check_budget(self, runtime: ModuleRuntime, latency_ms: float) -> ModuleFailure | None:
        if latency_ms <= self._config.observe_budget_ms:
            runtime.consecutive_overruns = 0
            return None
        runtime.consecutive_overruns += 1
        runtime.overruns_total += 1
        self._metrics.counter(
            "vigil_module_budget_overruns_total",
            camera=self._camera_id,
            module=runtime.module_id,
        ).inc()
        if runtime.consecutive_overruns < CONSECUTIVE_BUDGET_VIOLATIONS_TO_SUSPEND:
            self._log.warning(
                "module_over_budget",
                module_id=runtime.module_id,
                latency_ms=latency_ms,
                budget_ms=self._config.observe_budget_ms,
                consecutive=runtime.consecutive_overruns,
            )
            return None
        runtime.suspended = True
        reason = (
            f"exceeded the {self._config.observe_budget_ms} ms observe budget on "
            f"{runtime.consecutive_overruns} consecutive frames (last {latency_ms:.1f} ms)"
        )
        runtime.last_error = reason
        self._log.error("module_suspended", module_id=runtime.module_id, reason=reason)
        return ModuleFailure(
            module_id=runtime.module_id, reason=reason, quarantined=False, suspended=True
        )

    def _leave_quarantine(self, runtime: ModuleRuntime) -> None:
        runtime.quarantined_until_ns = None
        runtime.consecutive_errors = 0
        self._log.info("module_retried_after_quarantine", module_id=runtime.module_id)

    # ---------------------------------------------------------------- epoch

    def _apply_epoch(self, scene: SceneState) -> bool:
        """Reset module state when the stream or tracker epoch changes (rule 4).

        Both are watched: the stream epoch bumps on reconnect, and the tracker epoch bumps on
        reset after a long gap. Either means the identities in this scene are not the
        identities in the last one.
        """
        tracker_epoch = max((t.tracker_epoch for t in scene.tracks), default=0)
        epoch = (scene.frame_meta.stream_epoch, tracker_epoch)
        if self._epoch is None:
            self._epoch = epoch
            return False
        if epoch == self._epoch:
            return False
        previous, self._epoch = self._epoch, epoch
        self._log.info("module_state_reset_on_epoch_change", previous=previous, current=epoch)
        for module_id in self.module_ids():
            runtime = self._runtimes[module_id]
            self._rebind(runtime)
        return True

    def _rebind(self, runtime: ModuleRuntime) -> None:
        """`release()` then `bind()` with the same context: the contract's own reset mechanism,
        so a module does not need a separate one to get right."""
        context = _context_for(runtime, self._camera_id, self._log)
        try:
            runtime.module.release()
            runtime.module.bind(context)
        except VigilError as exc:
            self._record_error(runtime, f"failed to reset on epoch change: {exc.message}", 0)
        except Exception as exc:  # noqa: BLE001 - rule 5 applies to reset as much as to observe
            self._record_error(
                runtime, f"failed to reset on epoch change: {type(exc).__name__}: {exc}", 0
            )

    def release(self) -> None:
        """Shut every module down. Never raises: shutdown must complete."""
        for module_id in self.module_ids():
            runtime = self._runtimes[module_id]
            try:
                runtime.module.release()
            except Exception as exc:  # noqa: BLE001 - a bad plugin must not block shutdown
                self._log.warning(
                    "module_release_failed",
                    module_id=module_id,
                    error=f"{type(exc).__name__}: {exc}",
                )


def _context_for(runtime: ModuleRuntime, camera_id: str, logger: LoggerLike) -> ModuleContext:
    return ModuleContext(camera_id=camera_id, params=runtime.params, logger=logger)
