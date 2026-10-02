from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigil.core.clock import ManualClock
from vigil.pipeline.scheduler import CameraPlan, InferenceScheduler


def make(
    clock: ManualClock, *plans: CameraPlan, global_fps: float = 1000.0, burst: int = 4
) -> InferenceScheduler:
    return InferenceScheduler(plans, global_fps=global_fps, global_burst=burst, clock=clock)


def live(cam: str, fps: float = 10.0, priority: int = 5) -> CameraPlan:
    return CameraPlan(cam, fps, priority, lossless=False)


def test_a_camera_may_be_served_immediately_then_must_wait_for_a_token(clock: ManualClock) -> None:
    s = make(clock, live("cam-a", fps=10))
    assert s.order() == ["cam-a"]
    s.consume("cam-a")
    assert s.order() == []  # no token left
    clock.advance_ms(99)
    assert s.order() == []  # 10 fps = one token per 100 ms
    clock.advance_ms(2)
    assert s.order() == ["cam-a"]


def test_the_served_rate_converges_on_the_target_rate(clock: ManualClock) -> None:
    s = make(clock, live("cam-a", fps=10))
    served = 0
    for _ in range(1000):  # 10 s in 10 ms ticks, a frame always waiting
        clock.advance_ms(10)
        if s.order():
            s.consume("cam-a")
            served += 1
    assert served == pytest.approx(100, abs=2)


def test_tokens_do_not_accumulate_beyond_one_so_there_is_no_burst_after_idling(
    clock: ManualClock,
) -> None:
    s = make(clock, live("cam-a", fps=10))
    s.consume("cam-a")
    clock.advance_ms(60_000)  # a long idle period
    assert s.order() == ["cam-a"]
    s.consume("cam-a")
    assert s.order() == []  # still only one frame, not sixty


def test_the_global_ceiling_caps_the_total_across_cameras(clock: ManualClock) -> None:
    s = make(clock, live("a1", 100), live("b2", 100), live("c3", 100), global_fps=10, burst=1)
    assert s.global_capacity() == 1
    s.consume("a1")
    assert s.global_capacity() == 0
    clock.advance_ms(100)
    assert s.global_capacity() == 1


def test_higher_priority_is_served_first(clock: ManualClock) -> None:
    s = make(clock, live("low1", priority=1), live("high", priority=9), live("mid1", priority=5))
    assert s.order() == ["high", "mid1", "low1"]


def test_a_low_priority_camera_is_not_starved(clock: ManualClock) -> None:
    """Equal priority rotates strictly; and a camera that has waited longest goes first."""
    s = make(clock, live("a1", 1000), live("b2", 1000), live("c3", 1000))
    order: list[str] = []
    for _ in range(9):
        clock.advance_ms(5)
        first = s.order()[0]
        s.consume(first)
        order.append(first)
    assert {c: order.count(c) for c in ("a1", "b2", "c3")} == {"a1": 3, "b2": 3, "c3": 3}


def test_lossless_cameras_bypass_admission_entirely(clock: ManualClock) -> None:
    replay = CameraPlan("replay", rate_fps=1.0, priority=5, lossless=True)
    s = make(clock, replay, live("cam-a", 1), global_fps=1, burst=1)
    for _ in range(50):
        assert "replay" in s.order()  # never throttled, whatever its nominal rate
        s.consume("replay")
    assert s.global_capacity() == 1  # and it never consumed the live budget


def test_a_camera_with_no_plan_is_never_served(clock: ManualClock) -> None:
    assert make(clock).order() == []


def test_invalid_global_settings_are_rejected(clock: ManualClock) -> None:
    with pytest.raises(ValueError):
        make(clock, live("a1"), global_fps=0)
    with pytest.raises(ValueError):
        make(clock, live("a1"), burst=0)


@given(st.lists(st.integers(1, 200), min_size=1, max_size=60))
def test_never_serves_faster_than_the_configured_rate(gaps_ms: list[int]) -> None:
    clock = ManualClock()
    s = make(clock, live("a1", fps=5))
    served, elapsed = 0, 0
    for g in gaps_ms:
        clock.advance_ms(g)
        elapsed += g
        if s.order():
            s.consume("a1")
            served += 1
    assert served <= 1 + elapsed / 1000 * 5 + 1e-6  # the first frame is free, then 5 per second
