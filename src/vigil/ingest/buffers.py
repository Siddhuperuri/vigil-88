"""Bounded hand-off between the capture worker and its consumer (01 §9).

Each edge has one declared policy:
  * LatestFrameSlot     - live sources. Capacity 1, overwrite. Overwrites are *skipped* frames:
                          the system working as designed, counted separately from *dropped*.
  * BlockingFrameQueue  - paced sources (file replay). Bounded; the producer blocks, so
                          nothing is ever lost and replay stays reproducible.
"""

from __future__ import annotations

import threading
from collections import deque
from enum import StrEnum
from typing import Protocol

from vigil.domain.frame import Frame

_STOP_POLL_S = 0.05


class PutOutcome(StrEnum):
    STORED = "stored"
    OVERWROTE = "overwrote"  # a not-yet-consumed frame was replaced: count as skipped
    STOPPED = "stopped"  # a blocked put was aborted by the stop signal
    CLOSED = "closed"


class FrameChannel(Protocol):
    @property
    def lossless(self) -> bool:
        """True if frames must never be skipped (replay), so admission control is bypassed."""
        ...

    def put(self, frame: Frame, stop: threading.Event) -> PutOutcome: ...

    def take(self, timeout_s: float) -> Frame | None: ...

    def depth(self) -> int: ...

    def close(self) -> None: ...


class LatestFrameSlot:
    lossless = False

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._closed = False

    def put(self, frame: Frame, stop: threading.Event) -> PutOutcome:
        with self._cond:
            if self._closed:
                return PutOutcome.CLOSED
            outcome = PutOutcome.OVERWROTE if self._frame is not None else PutOutcome.STORED
            self._frame = frame
            self._cond.notify()
            return outcome

    def take(self, timeout_s: float) -> Frame | None:
        with self._cond:
            self._cond.wait_for(lambda: self._frame is not None or self._closed, timeout_s)
            frame, self._frame = self._frame, None
            return frame

    def depth(self) -> int:
        with self._cond:
            return 0 if self._frame is None else 1

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class BlockingFrameQueue:
    lossless = True

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._cond = threading.Condition()
        self._items: deque[Frame] = deque()
        self._closed = False

    def put(self, frame: Frame, stop: threading.Event) -> PutOutcome:
        with self._cond:
            while len(self._items) >= self._maxsize:
                if self._closed:
                    return PutOutcome.CLOSED
                if stop.is_set():
                    return PutOutcome.STOPPED
                self._cond.wait(_STOP_POLL_S)
            if self._closed:
                return PutOutcome.CLOSED
            self._items.append(frame)
            self._cond.notify()
            return PutOutcome.STORED

    def take(self, timeout_s: float) -> Frame | None:
        with self._cond:
            self._cond.wait_for(lambda: bool(self._items) or self._closed, timeout_s)
            if not self._items:
                return None
            frame = self._items.popleft()
            self._cond.notify()
            return frame

    def depth(self) -> int:
        with self._cond:
            return len(self._items)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
