"""The accumulator table for one camera, keyed by `(module_id, subject)` (04 §6.1).

Candidate identity is the whole point of this module. Two people in one restricted zone are
two hypotheses, not one twice as strong, so they must not share an accumulator; and one person
standing in that zone for a minute is one hypothesis, so they must not get a fresh accumulator
every frame. Both failures are easy to write and hard to see, so the key is defined once here
and nowhere else:

    camera_id   one store per camera, so the camera is implicit
    module_id   a module's evidence is its own; intrusion and loitering accumulate separately
    subject     whatever the operator would point at and name (06 §3)

Owned by one thread, the camera's analysis thread (D-005). No locks.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from vigil.config.schema.modules import TemporalConfig
from vigil.core.units import MS_PER_S
from vigil.domain.event import AccumulatorState
from vigil.domain.observation import Observation, SubjectKey
from vigil.temporal.accumulator import Accumulator

AccumulatorKey = tuple[str, SubjectKey]


def slot_ms_for(config: TemporalConfig, target_inference_fps: float) -> float:
    """The expected observation spacing: configured, or derived from the camera's inference rate.

    Deriving it is the honest default — observations arrive once per analysed frame, so the
    inference rate *is* the cadence — but a camera whose module only speaks up occasionally
    needs to say so rather than fail every support test.
    """
    if config.support_slot_ms is not None:
        return config.support_slot_ms
    return MS_PER_S / target_inference_fps


class AccumulatorStore:
    """Creates accumulators on first evidence and discards them when they go idle."""

    __slots__ = ("_accumulators", "_camera_id", "_config", "_evicted", "_slot_ms")

    def __init__(self, *, camera_id: str, config: TemporalConfig, support_slot_ms: float) -> None:
        self._camera_id = camera_id
        self._config = config
        self._slot_ms = support_slot_ms
        self._accumulators: dict[AccumulatorKey, Accumulator] = {}
        self._evicted = 0

    def __len__(self) -> int:
        return len(self._accumulators)

    def __iter__(self) -> Iterator[Accumulator]:
        return iter(self._accumulators.values())

    @property
    def evicted_total(self) -> int:
        return self._evicted

    def keys(self) -> tuple[AccumulatorKey, ...]:
        """Deterministic order: dict order is insertion order, which depends on arrival, so
        anything that iterates for output sorts instead (09 §9)."""
        return tuple(sorted(self._accumulators, key=lambda k: (k[0], k[1].kind, k[1].value)))

    def get(self, module_id: str, subject: SubjectKey) -> Accumulator | None:
        return self._accumulators.get((module_id, subject))

    def observe(self, obs: Observation) -> AccumulatorState:
        """Route one observation to its accumulator, creating it on first evidence."""
        key = (obs.module_id, obs.subject)
        accumulator = self._accumulators.get(key)
        if accumulator is None:
            accumulator = Accumulator(
                camera_id=self._camera_id,
                module_id=obs.module_id,
                subject=obs.subject,
                config=self._config,
                support_slot_ms=self._slot_ms,
            )
            self._accumulators[key] = accumulator
        return accumulator.observe(obs)

    def decay_all_to(self, t_monotonic_ns: int) -> None:
        """Apply the passage of time to every accumulator, including unobserved ones.

        Without this an accumulator that stops receiving evidence would freeze at its last
        value and stay influential forever: decay has to be driven by the tick, not only by
        the next observation.
        """
        for accumulator in self._accumulators.values():
            current = accumulator.current_ns
            if current is not None and t_monotonic_ns >= current:
                accumulator.decay_to(t_monotonic_ns)

    def prune_idle(self, t_monotonic_ns: int) -> tuple[AccumulatorKey, ...]:
        """Drop inactive accumulators whose evidence has gone stale. Returns what was dropped.

        An active one is never pruned however quiet it is: it is released by hysteresis, which
        is a decision with a reason, not by a timeout that loses the thread of the story.
        """
        stale = tuple(
            key for key in self.keys() if self._accumulators[key].is_idle_at(t_monotonic_ns)
        )
        for key in stale:
            del self._accumulators[key]
        self._evicted += len(stale)
        return stale

    def promotion_ready(self, t_monotonic_ns: int) -> Sequence[Accumulator]:
        """Accumulators that satisfy every promotion condition, in deterministic order."""
        return [
            self._accumulators[key]
            for key in self.keys()
            if self._accumulators[key].promotion_ready(t_monotonic_ns)
        ]

    def release_ready(self, t_monotonic_ns: int) -> Sequence[Accumulator]:
        return [
            self._accumulators[key]
            for key in self.keys()
            if self._accumulators[key].release_ready(t_monotonic_ns)
        ]

    def reset(self) -> None:
        """Forget everything. Called on a tracker epoch change: identities no longer mean what
        they meant, so evidence keyed by them is worthless and keeping it would attribute one
        object's history to another (04 §3.2)."""
        self._evicted += len(self._accumulators)
        self._accumulators.clear()
