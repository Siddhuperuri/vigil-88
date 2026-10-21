"""The two bounded edges between workers, and the policies they declare (01 §9).

Both of the behaviours asserted here were defects before P6:

  * the inference-to-analysis edge dropped frames for **every** camera regardless of timing, so
    a PACED source lost frames and replay stopped being reproducible — 07 §2 makes losslessness
    the property that defines PACED ("Replay must be lossless or results are not reproducible").
    `vigil run --selftest` was losing 27 of its 60 frames, and nobody knew, because the drop
    counter was not wired either (ADR 0015 §B);
  * `SpillQueue` counted a spill outside the lock, so concurrent spills lost counts — under
    exactly the sustained overload the counter exists to measure.
"""

from __future__ import annotations

import threading

from vigil.pipeline.queues import RoundRobinDropQueue, SpillQueue

# ------------------------------------------------------------------ LIVE: drop the oldest


def test_a_put_under_the_bound_drops_nothing() -> None:
    q: RoundRobinDropQueue[int] = RoundRobinDropQueue(2)
    assert q.put("cam-a", 1) is None
    assert q.put("cam-a", 2) is None
    assert q.depth("cam-a") == 2


def test_a_full_queue_drops_the_oldest_and_says_so() -> None:
    """The recent picture beats the complete one when a live camera is overloaded — but the
    drop is reported, because a silent drop is indistinguishable from a bug."""
    q: RoundRobinDropQueue[int] = RoundRobinDropQueue(2)
    q.put("cam-a", 1)
    q.put("cam-a", 2)
    report = q.put("cam-a", 3)
    assert report is not None
    assert (report.camera_id, report.dropped) == ("cam-a", 1)
    assert [q.take(0.0), q.take(0.0)] == [2, 3]


def test_the_bound_is_per_camera() -> None:
    """A shared bound lets a 30 fps camera evict a 2 fps camera's only frame, which looks
    exactly like the slow camera's detector failing."""
    q: RoundRobinDropQueue[int] = RoundRobinDropQueue(1)
    assert q.put("cam-a", 1) is None
    assert q.put("cam-b", 2) is None
    assert q.depth() == 2


# ------------------------------------------------------------------ PACED: block, never drop


def test_a_blocking_put_waits_for_room_instead_of_dropping() -> None:
    q: RoundRobinDropQueue[int] = RoundRobinDropQueue(2)
    q.put("cam-a", 1)
    q.put("cam-a", 2)
    done = threading.Event()

    def producer() -> None:
        assert q.put("cam-a", 3, block=True) is None
        done.set()

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    assert not done.wait(0.1), "the producer should still be waiting on a full queue"

    first = q.take(0.5)  # makes room
    assert done.wait(2.0), "taking an item must wake the blocked producer"
    thread.join(2.0)

    rest = [q.take(0.5), q.take(0.5)]
    assert [first, *rest] == [1, 2, 3]  # nothing was evicted


def test_a_blocking_put_is_released_by_closing_the_queue() -> None:
    """What makes the wait safe without a timeout: shutdown closes the queue, so a blocked
    inference worker is released rather than hanging the process (`pipeline/shutdown.py` step 2).
    """
    q: RoundRobinDropQueue[int] = RoundRobinDropQueue(1)
    q.put("cam-a", 1)
    released = threading.Event()

    def producer() -> None:
        assert q.put("cam-a", 2, block=True) is None
        released.set()

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    assert not released.wait(0.1)
    q.close()
    assert released.wait(2.0), "close() must release a blocked producer"
    thread.join(2.0)
    # The item was not smuggled in behind the close.
    assert q.take(0.0) == 1
    assert q.take(0.0) is None


def test_a_blocking_put_on_an_already_closed_queue_returns_immediately() -> None:
    q: RoundRobinDropQueue[int] = RoundRobinDropQueue(1)
    q.close()
    assert q.put("cam-a", 1, block=True) is None
    assert q.depth() == 0


def test_a_blocked_producer_does_not_stop_another_camera_being_served() -> None:
    """The bound is per camera, so one stalled camera must not take the others down with it."""
    q: RoundRobinDropQueue[int] = RoundRobinDropQueue(1)
    q.put("cam-a", 1)
    started = threading.Event()

    def producer() -> None:
        started.set()
        q.put("cam-a", 2, block=True)

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    assert started.wait(2.0)
    assert q.put("cam-b", 10) is None
    assert q.depth("cam-b") == 1
    q.close()
    thread.join(2.0)


# ------------------------------------------------------------------ the spill counter


def test_spilled_items_are_counted_once_each_under_concurrency() -> None:
    received: list[int] = []
    lock = threading.Lock()

    def sink(item: int) -> None:
        with lock:
            received.append(item)

    q: SpillQueue[int] = SpillQueue(1, spill=sink)
    assert q.put(0, timeout_s=0.0) is True  # fills it

    def spill_one(value: int) -> None:
        assert q.put(value, timeout_s=0.0) is False

    threads = [threading.Thread(target=spill_one, args=(i,), daemon=True) for i in range(1, 25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)

    assert len(received) == 24
    assert q.spilled_total == 24


def test_an_item_that_fits_is_not_spilled() -> None:
    spilled: list[int] = []
    q: SpillQueue[int] = SpillQueue(2, spill=spilled.append)
    assert q.put(1, timeout_s=0.0) is True
    assert spilled == []
    assert q.spilled_total == 0
