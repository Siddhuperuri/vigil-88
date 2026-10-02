"""Periodic camera health evaluation (07 §5, stall detection).

A stalled stream is worse than a disconnected one: the socket or device stays open and
everything looks healthy. The watchdog marks it DEGRADED ("stalled"), which makes
verification reject candidates derived from it.

It does not force-close a blocked `read()`: closing an OpenCV capture from another thread is
not safe (docs/LIMITATIONS.md). Recovery happens cooperatively when the read returns.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

from vigil.ingest.health import CameraHealthTracker


class Watchdog:
    def __init__(self, trackers: Sequence[CameraHealthTracker], *, interval_ms: int) -> None:
        self._trackers = tuple(trackers)
        self._interval_s = interval_ms / 1000.0

    def tick(self) -> None:
        for tracker in self._trackers:
            tracker.evaluate()

    def run(self, stop: threading.Event, beat: Callable[[], None]) -> None:
        while not stop.is_set():
            beat()
            self.tick()
            stop.wait(self._interval_s)
