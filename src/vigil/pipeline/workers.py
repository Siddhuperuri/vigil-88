"""Thread wrapper with liveness beacon and crash capture.

A crashing worker is never silent: the exception is logged at ERROR with its traceback,
counted, and surfaced through `health()`. There is no automatic restart in P0 (the
supervisor with its circuit breaker is a later phase); a crashed worker makes the
application report FAILED health.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from vigil.core.clock import Clock
from vigil.core.errors import LifecycleError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import ns_to_ms
from vigil.observability.metrics import MetricsRegistry

WorkerTarget = Callable[[threading.Event, Callable[[], None]], None]
THREAD_PREFIX = "vigil-"


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    name: str
    started: bool
    alive: bool
    crashed: bool
    error: str | None
    heartbeat_age_ms: float | None


class ThreadWorker:
    def __init__(
        self,
        name: str,
        target: WorkerTarget,
        *,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self.name = name
        self._target = target
        self._clock = clock
        self._log = logger.bind(worker=name)
        self._metrics = metrics
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_beat_ns: int | None = None
        self._crash: Exception | None = None

    def _beat(self) -> None:
        self._last_beat_ns = self._clock.monotonic_ns()

    def _run(self) -> None:
        try:
            self._target(self._stop, self._beat)
        except Exception as exc:  # noqa: BLE001 - top-level thread boundary: log, count, surface
            self._crash = exc
            self._metrics.counter("vigil_worker_crashes_total", worker=self.name).inc()
            self._log.error("worker crashed", error=repr(exc), exc_info=True)

    def start(self) -> None:
        if self._thread is not None:
            raise LifecycleError(f"worker {self.name!r} was already started")
        self._thread = threading.Thread(
            target=self._run, name=f"{THREAD_PREFIX}{self.name}", daemon=True
        )
        self._beat()
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def join(self, timeout_s: float) -> bool:
        """True if the thread has finished (or never started)."""
        if self._thread is None:
            return True
        self._thread.join(timeout_s)
        return not self._thread.is_alive()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def health(self) -> WorkerHealth:
        beat = self._last_beat_ns
        age = None if beat is None else ns_to_ms(self._clock.monotonic_ns() - beat)
        return WorkerHealth(
            name=self.name,
            started=self._thread is not None,
            alive=self.alive,
            crashed=self._crash is not None,
            error=repr(self._crash) if self._crash is not None else None,
            heartbeat_age_ms=age,
        )
