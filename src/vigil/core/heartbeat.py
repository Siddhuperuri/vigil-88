"""Waiting without going silent.

A worker's heartbeat is what `pipeline/health.py` reads to tell "blocked" from "busy": a beat
that is minutes old means the thread is stuck in a call that never returned. That inference only
holds if every worker's *normal* cadence is well inside the stall budget — and the retention
worker sleeps `retention.sweep_interval_s` between sweeps, an hour by default.

The first version of the stall check learned this the hard way: it reported the retention worker
stalled 30 s into every benchmark run, and the benchmark correctly aborted. The fix is not a
per-worker threshold, which would make the budget a tuning exercise and would still have to be
kept in step with each worker's interval. It is to make the beacon mean what the check needs it
to mean — *this thread is responsive* — by beating while waiting.

So a long wait is served in slices with a beat between them. The cost is one wake per slice on
an otherwise idle thread; the gain is that one stall budget is correct for every worker.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

# Long enough to be negligible on an idle thread, short enough that any sane stall budget is a
# large multiple of it.
HEARTBEAT_SLICE_S = 1.0


def wait_beating(
    stop: threading.Event,
    beat: Callable[[], None],
    seconds: float,
    *,
    slice_s: float = HEARTBEAT_SLICE_S,
) -> bool:
    """Wait up to `seconds` for `stop`, beating at least every `slice_s`.

    Returns True if `stop` was set, matching `threading.Event.wait`. No clock is read: the
    elapsed time is counted in slices, which is as precise as a sweep interval needs.
    """
    if seconds <= 0:
        return stop.is_set()
    remaining = seconds
    while remaining > 0:
        beat()
        if stop.wait(min(slice_s, remaining)):
            return True
        remaining -= slice_s
    return stop.is_set()
