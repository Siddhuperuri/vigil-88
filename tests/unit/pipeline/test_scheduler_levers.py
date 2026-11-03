"""The two levers the degradation ladder pulls on the scheduler."""

from __future__ import annotations

import math

import pytest

from vigil.core.clock import ManualClock
from vigil.pipeline.scheduler import CameraPlan, InferenceScheduler


def make(clock: ManualClock, *plans: CameraPlan) -> InferenceScheduler:
    return InferenceScheduler(plans, global_fps=1000.0, global_burst=4, clock=clock)


def live(cam: str, fps: float = 10.0, priority: int = 5) -> CameraPlan:
    return CameraPlan(cam, fps, priority, lossless=False)


def served_in(s: InferenceScheduler, clock: ManualClock, cam: str, seconds: int) -> int:
    served = 0
    for _ in range(seconds * 100):
        clock.advance_ms(10)
        if cam in s.order():
            s.consume(cam)
            served += 1
    return served


def test_halving_the_scale_halves_the_served_rate(clock: ManualClock) -> None:
    full = make(clock, live("a", 10))
    base = served_in(full, clock, "a", 10)
    half = make(clock, live("a", 10))
    half.set_rate_scale(0.5)
    assert abs(served_in(half, clock, "a", 10) - base / 2) <= 2


def test_a_suspended_camera_is_never_served_or_waited_for(clock: ManualClock) -> None:
    s = make(clock, live("a"), live("b"))
    s.set_suspended(frozenset({"a"}))
    assert s.order() == ["b"]
    s.consume("b")
    assert s.seconds_until_ready() == pytest.approx(0.1, abs=0.02)  # waits for b, not a


def test_with_everything_suspended_there_is_nothing_to_wait_for(clock: ManualClock) -> None:
    s = make(clock, live("a"))
    s.consume("a")
    s.set_suspended(frozenset({"a"}))
    assert s.order() == [] and math.isinf(s.seconds_until_ready())


def test_resuming_makes_the_camera_eligible_again(clock: ManualClock) -> None:
    s = make(clock, live("a"))
    s.set_suspended(frozenset({"a"}))
    s.set_suspended(frozenset())
    assert s.order() == ["a"]


def test_a_replayed_file_ignores_the_scale(clock: ManualClock) -> None:
    s = make(clock, CameraPlan("file", 5.0, 5, lossless=True))
    s.set_rate_scale(0.25)
    assert s.order() == ["file"]


@pytest.mark.parametrize("scale", [0.0, -1.0, 1.5])
def test_a_scale_outside_zero_to_one_is_refused(clock: ManualClock, scale: float) -> None:
    with pytest.raises(ValueError, match="scale"):
        make(clock, live("a")).set_rate_scale(scale)


def test_the_wait_lengthens_with_the_scale(clock: ManualClock) -> None:
    s = make(clock, live("a", 10))
    s.set_rate_scale(0.5)
    s.consume("a")
    assert s.seconds_until_ready() > 0.15
