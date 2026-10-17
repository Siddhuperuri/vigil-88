"""Bounded hand-off between the capture worker and its consumer (01 §9).

Each edge has one declared policy:
  * LatestFrameSlot     - live sources. Capacity 1, overwrite. Overwrites are *skipped* frames:
                          the system working as designed, counted separately from *dropped*.
  * BlockingFrameQueue  - paced sources (file replay). Bounded; the producer blocks, so
                          nothing is ever lost and replay stays reproducible.
  * PreRollBuffer       - evidence. A ring of JPEG-encoded frames, bounded by both a time
                          window and a byte ceiling, so an incident's evidence can begin
                          before the event that caused it.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from vigil.core.units import NS_PER_MS, ns_to_ms
from vigil.domain.frame import Frame, FrameMeta

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

    def __init__(self, wake: threading.Event | None = None) -> None:
        # `wake` is set on every put, so a consumer can sleep until a frame arrives instead of
        # polling (a fixed poll interval silently costs throughput).
        self._wake = wake
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
        if self._wake is not None:
            self._wake.set()
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

    def __init__(self, maxsize: int, wake: threading.Event | None = None) -> None:
        self._wake = wake
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
        if self._wake is not None:
            self._wake.set()
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


@dataclass(frozen=True, slots=True)
class EncodedFrame:
    """One JPEG-encoded frame and the metadata that says which frame it was."""

    meta: FrameMeta
    jpeg: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.jpeg)


class PreRollBuffer:
    """Per-camera ring of encoded frames, so evidence can start before the event (05 §6, Q3).

    **Encoded, not raw.** A 1080p frame is about 2.6 MB raw, so ten seconds across three cameras
    would blow the memory budget on its own; the same frames as JPEGs are a few hundred KB each.
    The cost is a re-encode later if a clip is muxed, which happens off the hot path.

    **Bounded twice, deliberately.** `window_ms` is what an operator asked for and `max_bytes` is
    what the machine can afford, and the tighter of the two always wins. A camera that suddenly
    produces much larger frames (a scene change, a quality bump) must not be able to turn a time
    bound into unbounded memory, which is the failure a single bound would allow.

    Written by the pre-roll sampler and read by the response worker, so it locks — but only
    around pointer moves, never around an encode or a disk write (01 §8 invariant 4).
    """

    __slots__ = ("_bytes", "_frames", "_lock", "_max_bytes", "_overwritten", "_window_ns")

    def __init__(self, *, window_ms: float, max_bytes: int) -> None:
        if window_ms <= 0.0:
            raise ValueError("PreRollBuffer.window_ms must be positive")
        if max_bytes < 1:
            raise ValueError("PreRollBuffer.max_bytes must be >= 1")
        self._window_ns = int(window_ms * NS_PER_MS)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._frames: deque[EncodedFrame] = deque()
        self._bytes = 0
        self._overwritten = 0

    def append(self, frame: EncodedFrame) -> int:
        """Add a frame and evict whatever no longer fits. Returns the number evicted."""
        with self._lock:
            self._frames.append(frame)
            self._bytes += frame.size_bytes
            evicted = self._evict_locked(frame.meta.t_monotonic_ns)
        self._overwritten += evicted
        return evicted

    def _evict_locked(self, now_ns: int) -> int:
        evicted = 0
        horizon = now_ns - self._window_ns
        # One frame always survives: a buffer that evicted its only frame would report "no
        # pre-roll available" for a camera that is working perfectly.
        while len(self._frames) > 1 and (
            self._frames[0].meta.t_monotonic_ns < horizon or self._bytes > self._max_bytes
        ):
            self._bytes -= self._frames.popleft().size_bytes
            evicted += 1
        return evicted

    def window(self, *, since_ns: int, until_ns: int) -> tuple[EncodedFrame, ...]:
        """Frames captured in `[since_ns, until_ns]`, oldest first. Never raises on an empty
        buffer: "no pre-roll" is a real answer and the caller reports it rather than inventing
        footage (06 rule: never fabricate missing evidence)."""
        with self._lock:
            return tuple(f for f in self._frames if since_ns <= f.meta.t_monotonic_ns <= until_ns)

    def latest(self) -> EncodedFrame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def clear(self) -> None:
        """Called on a stream-epoch change: frames from before a reconnect may be a different
        view, and presenting them as this incident's pre-roll would be misleading."""
        with self._lock:
            self._frames.clear()
            self._bytes = 0

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    @property
    def evicted_total(self) -> int:
        return self._overwritten

    @property
    def span_ms(self) -> float:
        with self._lock:
            if len(self._frames) < 2:
                return 0.0
            first, last = self._frames[0].meta, self._frames[-1].meta
            return ns_to_ms(last.t_monotonic_ns - first.t_monotonic_ns)
