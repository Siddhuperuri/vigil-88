from __future__ import annotations

import threading
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.support.fakes import StubPixels
from vigil.core.clock import ManualClock
from vigil.core.rng import seeded_rng
from vigil.domain import Frame, FrameMeta
from vigil.ingest.backoff import reconnect_delay_ms
from vigil.ingest.buffers import BlockingFrameQueue, LatestFrameSlot, PutOutcome

# ------------------------------------------------------------------ backoff


def delay(attempt: int, seed: int = 0, jitter: float = 0.2) -> float:
    return reconnect_delay_ms(
        attempt, base_ms=500, max_ms=30_000, jitter_ratio=jitter, rng=seeded_rng(seed)
    )


def test_backoff_grows_exponentially_without_jitter() -> None:
    assert [delay(a, jitter=0.0) for a in range(4)] == [500, 1000, 2000, 4000]


def test_backoff_is_capped() -> None:
    assert delay(20, jitter=0.0) == 30_000
    assert delay(10_000, jitter=0.0) == 30_000  # no overflow for absurd attempt counts


@given(st.integers(0, 80), st.integers(0, 1000))
def test_backoff_stays_within_jitter_bounds_and_never_exceeds_max(attempt: int, seed: int) -> None:
    capped = min(500 * 2 ** min(attempt, 62), 30_000)
    d = delay(attempt, seed)
    assert capped * 0.8 - 1e-6 <= d <= min(capped * 1.2, 30_000) + 1e-6


def test_backoff_jitter_desynchronises_cameras_but_is_reproducible() -> None:
    assert len({delay(3, seed) for seed in range(20)}) > 1  # different cameras differ
    assert delay(3, seed=5) == delay(3, seed=5)  # same seed, same result


def test_backoff_rejects_negative_attempts() -> None:
    with pytest.raises(ValueError):
        delay(-1)


# ------------------------------------------------------------------ buffers


def frame(i: int = 0) -> Frame:
    clock = ManualClock()
    meta = FrameMeta("cam-a", 1, i, 0, clock.wall_utc(), 64, 48, None, None)
    return Frame(meta, StubPixels())


def test_slot_overwrite_is_reported_so_it_can_be_counted_as_skipped() -> None:
    slot, stop = LatestFrameSlot(), threading.Event()
    assert slot.put(frame(0), stop) is PutOutcome.STORED
    assert slot.put(frame(1), stop) is PutOutcome.OVERWROTE
    assert slot.depth() == 1
    got = slot.take(0)
    assert got is not None and got.meta.frame_index == 1  # latest wins
    assert slot.depth() == 0


def test_slot_take_times_out_empty() -> None:
    assert LatestFrameSlot().take(0.01) is None


def test_slot_take_wakes_when_a_frame_arrives() -> None:
    slot, stop = LatestFrameSlot(), threading.Event()
    threading.Timer(0.05, lambda: slot.put(frame(7), stop)).start()
    got = slot.take(5.0)
    assert got is not None and got.meta.frame_index == 7


def test_slot_close_wakes_a_waiting_consumer_and_refuses_puts() -> None:
    slot, stop = LatestFrameSlot(), threading.Event()
    threading.Timer(0.05, slot.close).start()
    started = time.monotonic()
    assert slot.take(5.0) is None
    assert time.monotonic() - started < 2.0
    assert slot.put(frame(), stop) is PutOutcome.CLOSED


def test_queue_is_fifo_and_bounded() -> None:
    q, stop = BlockingFrameQueue(2), threading.Event()
    assert q.put(frame(0), stop) is PutOutcome.STORED
    assert q.put(frame(1), stop) is PutOutcome.STORED
    assert q.depth() == 2
    taken = [q.take(0), q.take(0)]
    assert [f.meta.frame_index for f in taken if f] == [0, 1]
    assert q.take(0.01) is None


def test_queue_put_blocks_until_space_instead_of_dropping() -> None:
    q, stop = BlockingFrameQueue(1), threading.Event()
    q.put(frame(0), stop)
    results: list[PutOutcome] = []
    t = threading.Thread(target=lambda: results.append(q.put(frame(1), stop)))
    t.start()
    time.sleep(0.1)
    assert t.is_alive() and results == []  # blocked, nothing lost
    q.take(0)
    t.join(5)
    assert results == [PutOutcome.STORED] and q.depth() == 1


def test_queue_blocked_put_is_aborted_by_stop() -> None:
    q, stop = BlockingFrameQueue(1), threading.Event()
    q.put(frame(0), stop)
    results: list[PutOutcome] = []
    t = threading.Thread(target=lambda: results.append(q.put(frame(1), stop)))
    t.start()
    stop.set()
    t.join(5)
    assert results == [PutOutcome.STOPPED]


def test_queue_blocked_put_is_aborted_by_close() -> None:
    q, stop = BlockingFrameQueue(1), threading.Event()
    q.put(frame(0), stop)
    results: list[PutOutcome] = []
    t = threading.Thread(target=lambda: results.append(q.put(frame(1), stop)))
    t.start()
    q.close()
    t.join(5)
    assert results == [PutOutcome.CLOSED]


def test_queue_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError):
        BlockingFrameQueue(0)
