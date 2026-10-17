"""The two bounded edges between workers, each with one declared policy (01 §9).

A queue whose behaviour under pressure is an incidental consequence of its `maxsize` is a
latent outage. Both policies here are chosen, named, and counted:

  * **inference to analysis — `DROP_OLDEST`, per camera.** Analysis falling behind means the
    system is overloaded, and in that state the recent picture is worth more than the complete
    one. Per camera rather than shared, because a shared bound lets a 30 fps camera evict a
    2 fps camera's only frame, which looks exactly like the slow camera's detector failing.
    Drops are counted per camera and surfaced; a silent drop is indistinguishable from a bug.

  * **analysis to response — `BLOCK` with timeout, then spill.** Losing a confirmed incident is
    unacceptable, so the producer waits; but it must not wait forever, because the thread that
    is waiting owns a camera's analysis and stalling it would stop detection on that camera. On
    timeout the item is handed to a spill sink (a disk journal) and counted, so the record
    survives even though the live path gave up on it.

No clock is read here: both queues take the waiting time as an argument.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

_POLL_S = 0.05


@dataclass(frozen=True, slots=True)
class DropReport:
    camera_id: str
    dropped: int


class RoundRobinDropQueue(Generic[T]):
    """Per-key bounded queues drained in rotation, oldest dropped when a key is full."""

    __slots__ = ("_closed", "_cond", "_maxsize", "_order", "_queues", "_wake")

    def __init__(self, maxsize: int, wake: threading.Event | None = None) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._wake = wake
        self._cond = threading.Condition()
        self._queues: dict[str, deque[T]] = {}
        self._order: deque[str] = deque()
        self._closed = False

    def put(self, key: str, item: T) -> DropReport | None:
        """Enqueue under `key`. Returns a report when something had to be dropped."""
        dropped = 0
        with self._cond:
            if self._closed:
                return None
            queue = self._queues.get(key)
            if queue is None:
                queue = deque()
                self._queues[key] = queue
                self._order.append(key)
            while len(queue) >= self._maxsize:
                queue.popleft()
                dropped += 1
            queue.append(item)
            self._cond.notify()
        if self._wake is not None:
            self._wake.set()
        return DropReport(key, dropped) if dropped else None

    def take(self, timeout_s: float) -> T | None:
        """The next item in key rotation, or None on timeout or close."""
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._any_pending(), timeout_s)
            for _ in range(len(self._order)):
                key = self._order[0]
                self._order.rotate(-1)
                queue = self._queues.get(key)
                if queue:
                    return queue.popleft()
            return None

    def _any_pending(self) -> bool:
        return any(self._queues.values())

    def depth(self, key: str | None = None) -> int:
        with self._cond:
            if key is not None:
                return len(self._queues.get(key, ()))
            return sum(len(q) for q in self._queues.values())

    def keys(self) -> tuple[str, ...]:
        with self._cond:
            return tuple(sorted(self._queues))

    def forget(self, key: str) -> None:
        with self._cond:
            self._queues.pop(key, None)
            if key in self._order:
                self._order.remove(key)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed


class SpillQueue(Generic[T]):
    """Bounded FIFO; a producer blocks up to `timeout_s`, then the item goes to the spill sink."""

    __slots__ = ("_closed", "_cond", "_items", "_maxsize", "_spill", "_spilled")

    def __init__(self, maxsize: int, *, spill: Callable[[T], None]) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._spill = spill
        self._cond = threading.Condition()
        self._items: deque[T] = deque()
        self._closed = False
        self._spilled = 0

    @property
    def spilled_total(self) -> int:
        return self._spilled

    def set_spill(self, spill: Callable[[T], None]) -> None:
        """Replace the spill sink after construction.

        The response worker is its own spill sink and needs the queue to exist first; the sink
        only logs and counts, so there is no runtime cycle (see `pipeline/response_stack.py`).
        """
        self._spill = spill

    def put(self, item: T, *, timeout_s: float, stop: threading.Event | None = None) -> bool:
        """True when the item entered the queue, False when it was spilled instead.

        The spill sink is called **outside** the lock: it writes to disk, and holding a queue
        lock across a disk write is how an unrelated stall becomes a pipeline stall (01 §8).
        """
        waited = 0.0
        with self._cond:
            while len(self._items) >= self._maxsize and not self._closed and waited < timeout_s:
                if stop is not None and stop.is_set():
                    break
                slice_s = min(_POLL_S, timeout_s - waited)
                self._cond.wait(slice_s)
                waited += slice_s
            if not self._closed and len(self._items) < self._maxsize:
                self._items.append(item)
                self._cond.notify()
                return True
        self._spilled += 1
        self._spill(item)
        return False

    def take(self, timeout_s: float) -> T | None:
        with self._cond:
            self._cond.wait_for(lambda: bool(self._items) or self._closed, timeout_s)
            if not self._items:
                return None
            item = self._items.popleft()
            self._cond.notify()
            return item

    def drain(self) -> tuple[T, ...]:
        """Everything queued, in order. Used at shutdown so nothing confirmed is lost."""
        with self._cond:
            items = tuple(self._items)
            self._items.clear()
            self._cond.notify_all()
            return items

    def depth(self) -> int:
        with self._cond:
            return len(self._items)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
