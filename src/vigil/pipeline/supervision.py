"""Restarting one worker without restarting the engine (plan D2: the inner ring).

The shell restarts the whole engine process when it dies. This is the smaller ring: a worker that
crashed, or stopped making progress, can be replaced while everything else keeps running.

**What it will not do, and says so.**

  * A thread cannot be killed from outside. A worker that is asked to stop and does not is *still
    running*, and starting a second copy beside it would double-read a camera or double-write the
    database. So a restart that cannot retire the old thread is refused, with the reason.
  * Some workers hold state that a thread swap would corrupt (the detector on the GPU, the
    analysis state, the queue of confirmed events). They are not restartable here; the message
    says the engine itself has to be restarted.
  * Restarting is bounded. A worker that dies again and again is not a worker a restart fixes, so
    after a few restarts in a window further ones are refused instead of looping.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from vigil.core.clock import Clock
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import NS_PER_S
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.workers import ThreadWorker

MAX_RESTARTS = 3
WINDOW_S = 600.0
COOLDOWN_S = 5.0
STOP_JOIN_S = 3.0


@dataclass(frozen=True, slots=True)
class RestartResult:
    ok: bool
    detail: str


@dataclass(slots=True)
class Entry:
    """How to find, replace and rebuild one worker."""

    name: str
    get: Callable[[], ThreadWorker | None]
    install: Callable[[ThreadWorker], None]
    rebuild: Callable[[], ThreadWorker] | None
    refusal: str = "this worker holds state that cannot be swapped; restart the engine instead"
    attempts: deque[int] = field(default_factory=deque)


class WorkerSupervisor:
    def __init__(
        self,
        *,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
        max_restarts: int = MAX_RESTARTS,
        window_s: float = WINDOW_S,
        cooldown_s: float = COOLDOWN_S,
        stop_join_s: float = STOP_JOIN_S,
    ) -> None:
        self._clock = clock
        self._log = logger.bind(stage="supervision")
        self._metrics = metrics
        self._max = max_restarts
        self._window_ns = int(window_s * NS_PER_S)
        self._cooldown_ns = int(cooldown_s * NS_PER_S)
        self._stop_join_s = stop_join_s
        self._entries: dict[str, Entry] = {}
        self._lock = threading.Lock()

    def register(self, entry: Entry) -> None:
        self._entries[entry.name] = entry

    def restartable(self, name: str) -> bool:
        entry = self._entries.get(name)
        return entry is not None and entry.rebuild is not None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def restart(self, name: str) -> RestartResult:
        # One restart at a time: two concurrent requests must not both retire and replace.
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return RestartResult(False, f"there is no worker named {name!r}")
            if entry.rebuild is None:
                return RestartResult(False, entry.refusal)
            refused = self._budget(entry)
            if refused is not None:
                return RestartResult(False, refused)
            return self._replace(entry, entry.rebuild)

    def _budget(self, entry: Entry) -> str | None:
        now = self._clock.monotonic_ns()
        while entry.attempts and now - entry.attempts[0] > self._window_ns:
            entry.attempts.popleft()
        if entry.attempts and now - entry.attempts[-1] < self._cooldown_ns:
            return "this worker was restarted moments ago; wait a few seconds and check its state"
        if len(entry.attempts) >= self._max:
            return (
                f"this worker has been restarted {self._max} times in the last "
                f"{self._window_ns // NS_PER_S // 60} minutes; a restart is not fixing it, "
                "restart the engine and read the log"
            )
        entry.attempts.append(now)
        return None

    def _replace(self, entry: Entry, rebuild: Callable[[], ThreadWorker]) -> RestartResult:
        old = entry.get()
        if old is not None and old.alive:
            old.request_stop()
            if not old.join(self._stop_join_s):
                self._metrics.counter("vigil_worker_restart_refused_total", worker=entry.name).inc()
                self._log.error("worker_restart_refused", worker=entry.name, reason="still running")
                return RestartResult(
                    False,
                    "the old thread is still running and cannot be stopped from outside, so a "
                    "second copy was not started; restart the engine",
                )
        try:
            fresh = rebuild()
        except Exception as exc:  # noqa: BLE001 - a rebuild can fail for any reason; say which
            self._log.error("worker_rebuild_failed", worker=entry.name, error=repr(exc))
            return RestartResult(False, f"the worker could not be rebuilt: {exc}")
        entry.install(fresh)
        fresh.start()
        self._metrics.counter("vigil_worker_restarts_total", worker=entry.name).inc()
        self._log.warning("worker_restarted", worker=entry.name)
        return RestartResult(True, "restarted")
