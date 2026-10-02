from __future__ import annotations

import math

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


def test_tokens_stop_accumulating_at_the_burst_cap_so_idling_buys_no_flood(
    clock: ManualClock,
) -> None:
    s = make(clock, live("cam-a", fps=10))  # default burst: 2 frames
    s.consume("cam-a")
    clock.advance_ms(60_000)  # a long idle period
    served = 0
    while s.order():
        s.consume("cam-a")
        served += 1
    assert served == 2  # the burst allowance, not the sixty frames that idling "earned"


def test_the_burst_cap_is_configurable_and_validated(clock: ManualClock) -> None:
    one = InferenceScheduler(
        [live("cam-a", 10)], global_fps=1000, global_burst=4, clock=clock, camera_burst=1.0
    )
    one.consume("cam-a")
    clock.advance_ms(60_000)
    assert one.order() == ["cam-a"]
    one.consume("cam-a")
    assert one.order() == []  # a cap of 1 allows no burst at all
    with pytest.raises(ValueError, match="camera_burst"):
        InferenceScheduler(
            [live("cam-a", 10)], global_fps=1, global_burst=1, clock=clock, camera_burst=0.5
        )


def test_a_camera_delivering_exactly_at_its_target_rate_is_served_every_frame(
    clock: ManualClock,
) -> None:
    """Regression: a 30 fps camera with a 30 fps target lost ~30% of its frames because a frame
    that arrived a hair before its token matured waited a poll interval and was overwritten."""
    s = make(clock, live("cam-a", fps=30))
    period_ms = 1000 / 30
    frame_waiting, served, offered = False, 0, 0
    for step in range(3000):  # 3 s at 1 ms resolution
        if step % round(period_ms) == 0 and step > 0:
            frame_waiting, offered = True, offered + 1  # a new frame replaces any unserved one
        clock.advance_ms(1)
        if frame_waiting and s.order():
            s.consume("cam-a")
            served += 1
            frame_waiting = False
    assert served >= offered * 0.97


def test_seconds_until_ready_tells_a_worker_how_long_it_may_sleep(clock: ManualClock) -> None:
    s = make(clock, live("cam-a", fps=10), global_fps=1000)
    assert s.seconds_until_ready() == math.inf  # a token is ready: waiting for a frame, not a token
    s.consume("cam-a")
    assert s.seconds_until_ready() == pytest.approx(0.1, abs=0.01)  # one token per 100 ms
    clock.advance_ms(40)
    assert s.seconds_until_ready() == pytest.approx(0.06, abs=0.01)


def test_seconds_until_ready_ignores_lossless_cameras_and_is_never_a_spin_loop(
    clock: ManualClock,
) -> None:
    replay = CameraPlan("replay", rate_fps=1.0, priority=5, lossless=True)
    assert make(clock, replay).seconds_until_ready() == math.inf
    s = make(clock, live("cam-a", fps=1_000_000))
    s.consume("cam-a")
    s.consume("cam-a")
    assert s.seconds_until_ready() >= 0.0005  # floored: never a busy loop


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
