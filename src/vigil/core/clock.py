"""Clocks (01 §7.6). The only module allowed to read real time.

Decision logic reads timestamps carried on frames, which were stamped from an injected
Clock. `time.time()`, `datetime.now()` and friends are banned elsewhere (scripts/gates.py).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

from vigil.core.units import ms_to_ns, ns_to_ms

DEFAULT_MANUAL_EPOCH: datetime = datetime(2026, 1, 1, tzinfo=UTC)


class Clock(Protocol):
    def monotonic_ns(self) -> int:
        """Arithmetic and ordering. Never jumps. Meaningless to a human."""
        ...

    def wall_utc(self) -> datetime:
        """Display and persistence. Timezone-aware UTC. May jump."""
        ...


class SeekableClock(Clock, Protocol):
    """A clock driven by media presentation timestamps (replay)."""

    def seek_pts_ms(self, pts_ms: float) -> None: ...


class SystemClock:
    """Production clock.

    `perf_counter_ns` rather than `monotonic_ns`: on Windows the latter ticks at about 15.6 ms,
    which quantises every latency measurement (stage times came out as 0 or 16 ms). The
    performance counter is monotonic and has sub-microsecond resolution.
    """

    def monotonic_ns(self) -> int:
        return time.perf_counter_ns()

    def wall_utc(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Test clock. Advances only when told to; never sleeps."""

    def __init__(
        self, *, wall_start: datetime = DEFAULT_MANUAL_EPOCH, monotonic_start_ns: int = 0
    ) -> None:
        if wall_start.tzinfo is None:
            raise ValueError("wall_start must be timezone-aware")
        self._lock = threading.Lock()
        self._mono_ns = monotonic_start_ns
        self._wall_start = wall_start.astimezone(UTC)
        self._mono_start_ns = monotonic_start_ns

    def advance_ns(self, ns: int) -> None:
        if ns < 0:
            raise ValueError("a clock cannot go backwards")
        with self._lock:
            self._mono_ns += ns

    def advance_ms(self, ms: float) -> None:
        self.advance_ns(ms_to_ns(ms))

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._mono_ns

    def wall_utc(self) -> datetime:
        with self._lock:
            elapsed_ms = ns_to_ms(self._mono_ns - self._mono_start_ns)
        return self._wall_start + timedelta(milliseconds=elapsed_ms)


class ReplayClock:
    """Time derived from media PTS so replay at any speed matches live behaviour (07 §4)."""

    def __init__(self, *, wall_anchor: datetime, monotonic_anchor_ns: int = 0) -> None:
        if wall_anchor.tzinfo is None:
            raise ValueError("wall_anchor must be timezone-aware")
        self._lock = threading.Lock()
        self._wall_anchor = wall_anchor.astimezone(UTC)
        self._mono_anchor_ns = monotonic_anchor_ns
        self._offset_ns = 0

    def seek_pts_ms(self, pts_ms: float) -> None:
        offset = ms_to_ns(pts_ms)
        with self._lock:
            if offset < self._offset_ns:
                raise ValueError(
                    f"PTS went backwards ({pts_ms} ms); a rewind must start a new stream epoch"
                )
            self._offset_ns = offset

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._mono_anchor_ns + self._offset_ns

    def wall_utc(self) -> datetime:
        with self._lock:
            offset_ns = self._offset_ns
        return self._wall_anchor + timedelta(milliseconds=ns_to_ms(offset_ns))
