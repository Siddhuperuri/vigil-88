"""Ordered-pattern matching over observation history (04 §6.4).

Some events are orderings rather than states: *vehicle stops, then a person exits, then the
vehicle departs without them*. Accumulation alone cannot express that, because it is
deliberately indifferent to the order evidence arrived in.

A match emits one high-signal `Observation`, which then goes through accumulation like
everything else. There is exactly one promotion mechanism, and a sequence does not get to skip
it: a matched pattern is strong evidence, not a verdict.

Time comes from the marks. No clock is read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from vigil.core.errors import TemporalError
from vigil.core.units import ns_to_ms
from vigil.domain.observation import FactValue, Observation, SubjectKey

_NO_FACTS: Mapping[str, FactValue] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Step:
    """One stage of a pattern. `min_duration_ms` requires the condition to *hold*, not merely
    occur: "stopped" for 3 s is a different fact from "stopped" for one frame."""

    name: str
    min_duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Step.name must not be empty")
        if self.min_duration_ms < 0.0:
            raise ValueError("Step.min_duration_ms must not be negative")


@dataclass(frozen=True, slots=True)
class SequencePattern:
    steps: tuple[Step, ...]
    within_ms: float
    allow_gaps: bool = True

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("a SequencePattern needs at least one step")
        if self.within_ms <= 0.0:
            raise ValueError("SequencePattern.within_ms must be positive")


@dataclass(frozen=True, slots=True)
class Mark:
    """A named condition observed to hold at an instant, for one subject."""

    name: str
    t_monotonic_ns: int


@dataclass(slots=True)
class _Progress:
    step_index: int = 0
    started_ns: int | None = None  # when step 0 first held
    step_first_ns: int | None = None  # when the current step first held
    last_mark_ns: int | None = None
    completed: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SequenceMatch:
    subject: SubjectKey
    pattern_id: str
    started_monotonic_ns: int
    matched_monotonic_ns: int

    @property
    def elapsed_ms(self) -> float:
        return ns_to_ms(self.matched_monotonic_ns - self.started_monotonic_ns)


class SequenceMatcher:
    """Tracks progress through one pattern, independently per subject.

    State per subject is one small record, reset when the window expires or the order breaks,
    so memory is bounded by the number of live subjects — the contract every module must obey
    (04 §3.5 rule 4).
    """

    __slots__ = ("_pattern", "_pattern_id", "_progress")

    def __init__(self, pattern_id: str, pattern: SequencePattern) -> None:
        if not pattern_id:
            raise ValueError("pattern_id must not be empty")
        self._pattern_id = pattern_id
        self._pattern = pattern
        self._progress: dict[SubjectKey, _Progress] = {}

    @property
    def pattern_id(self) -> str:
        return self._pattern_id

    def __len__(self) -> int:
        return len(self._progress)

    def reset(self) -> None:
        self._progress.clear()

    def forget(self, subject: SubjectKey) -> None:
        self._progress.pop(subject, None)

    def advance(self, subject: SubjectKey, mark: Mark) -> SequenceMatch | None:
        """Feed one mark. Returns a match when the final step completes, else None."""
        progress = self._progress.get(subject)
        if progress is None:
            progress = _Progress()
            self._progress[subject] = progress
        if progress.last_mark_ns is not None and mark.t_monotonic_ns < progress.last_mark_ns:
            raise TemporalError(
                "sequence marks must arrive in time order",
                context={"last_ns": progress.last_mark_ns, "got_ns": mark.t_monotonic_ns},
            )
        progress.last_mark_ns = mark.t_monotonic_ns
        if progress.started_ns is not None and (
            ns_to_ms(mark.t_monotonic_ns - progress.started_ns) > self._pattern.within_ms
        ):
            self._progress[subject] = progress = _Progress(last_mark_ns=mark.t_monotonic_ns)

        step = self._pattern.steps[progress.step_index]
        if mark.name != step.name:
            # An unexpected mark breaks a strict pattern; a gap-tolerant one simply waits.
            if not self._pattern.allow_gaps:
                self._progress[subject] = _Progress(last_mark_ns=mark.t_monotonic_ns)
            return None

        if progress.step_first_ns is None:
            progress.step_first_ns = mark.t_monotonic_ns
        if progress.started_ns is None:
            progress.started_ns = mark.t_monotonic_ns
        held_ms = ns_to_ms(mark.t_monotonic_ns - progress.step_first_ns)
        if held_ms < step.min_duration_ms:
            return None

        progress.completed.append(step.name)
        progress.step_index += 1
        progress.step_first_ns = None
        if progress.step_index < len(self._pattern.steps):
            return None

        started = progress.started_ns
        del self._progress[subject]
        return SequenceMatch(
            subject=subject,
            pattern_id=self._pattern_id,
            started_monotonic_ns=started,
            matched_monotonic_ns=mark.t_monotonic_ns,
        )


def observation_from_match(
    match: SequenceMatch,
    *,
    camera_id: str,
    module_id: str,
    signal: float,
    facts: Mapping[str, FactValue] = _NO_FACTS,
) -> Observation:
    """Turn a match into the one observation it is worth. It accumulates like any other."""
    merged: dict[str, FactValue] = {
        "sequence": match.pattern_id,
        "sequence_elapsed_ms": match.elapsed_ms,
        **facts,
    }
    return Observation(
        camera_id=camera_id,
        module_id=module_id,
        subject=match.subject,
        signal=signal,
        t_monotonic_ns=match.matched_monotonic_ns,
        facts=merged,
    )


def marks_from_names(names: Sequence[str], *, t_monotonic_ns: int) -> tuple[Mark, ...]:
    """Convenience for a module that computes several conditions for one frame."""
    return tuple(Mark(name, t_monotonic_ns) for name in names)
