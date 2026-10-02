"""Accumulator interface (04 §3.4, D-007). Implementation arrives in P3."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vigil.domain.event import AccumulatorState
    from vigil.domain.observation import Observation


class Accumulator(Protocol):
    """The temporal engine owns time, doubt and confirmation. Time comes from observations."""

    @property
    def state(self) -> AccumulatorState: ...

    def observe(self, obs: Observation) -> AccumulatorState: ...

    def decay_to(self, t_monotonic_ns: int) -> AccumulatorState: ...
