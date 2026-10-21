"""`wait_beating`: a long wait that does not look like a hung thread.

The stall check in `pipeline/health.py` infers "blocked" from an old heartbeat. That inference
is only sound while every worker's normal cadence is well inside the stall budget, and the
retention worker sleeps `retention.sweep_interval_s` — an hour by default — between sweeps. The
first version of the stall check reported it stalled 30 s into every benchmark run, and the
benchmark correctly aborted the run (ADR 0015 §B).
"""

from __future__ import annotations

import threading

from vigil.core.heartbeat import HEARTBEAT_SLICE_S, wait_beating


def test_it_beats_several_times_across_a_long_wait() -> None:
    """The point of the whole thing: silence is what the health check cannot interpret."""
    beats = 0

    def beat() -> None:
        nonlocal beats
        beats += 1

    stop = threading.Event()
    assert wait_beating(stop, beat, 0.5, slice_s=0.01) is False
    assert beats >= 10


def test_it_returns_as_soon_as_stop_is_set() -> None:
    """A worker must not owe the shutdown budget a whole sweep interval."""
    stop = threading.Event()
    beats: list[int] = []
    stop.set()
    assert wait_beating(stop, lambda: beats.append(1), 3600.0, slice_s=0.01) is True
    # One beat at most: it checked, found the event set and left.
    assert len(beats) <= 1


def test_it_stops_waiting_mid_slice_when_stop_is_set_from_another_thread() -> None:
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    assert wait_beating(stop, lambda: None, 10.0, slice_s=0.01) is True


def test_a_zero_or_negative_wait_does_not_beat_or_block() -> None:
    beats: list[int] = []
    stop = threading.Event()
    assert wait_beating(stop, lambda: beats.append(1), 0.0) is False
    assert wait_beating(stop, lambda: beats.append(1), -5.0) is False
    assert beats == []


def test_a_zero_wait_still_reports_a_stop_that_was_already_set() -> None:
    """Matches `Event.wait`: the return value is whether the event is set, not whether we slept."""
    stop = threading.Event()
    stop.set()
    assert wait_beating(stop, lambda: None, 0.0) is True


def test_the_default_slice_is_short_enough_to_be_a_small_fraction_of_any_stall_budget() -> None:
    """`PipelineConfig.worker_stall_timeout_ms` has a floor of 1000 ms; the slice must sit well
    inside it, or a worker could be declared stalled between two legitimate beats."""
    from vigil.config.schema.pipeline import PipelineConfig

    assert PipelineConfig().worker_stall_timeout_ms >= HEARTBEAT_SLICE_S * 1000.0 * 5
