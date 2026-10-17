"""The ordered-pattern matcher (04 §6.4). A match is strong evidence, never a verdict."""

from __future__ import annotations

import pytest

from vigil.core.errors import TemporalError
from vigil.core.units import ms_to_ns
from vigil.domain.observation import SubjectKey
from vigil.temporal.sequence import (
    Mark,
    SequenceMatcher,
    SequencePattern,
    Step,
    marks_from_names,
    observation_from_match,
)

SUBJECT = SubjectKey("track", "7")
OTHER = SubjectKey("track", "8")

PATTERN = SequencePattern(
    steps=(Step("stopped", min_duration_ms=3000.0), Step("person_exit"), Step("departed")),
    within_ms=120_000.0,
    allow_gaps=True,
)


def matcher(pattern: SequencePattern = PATTERN) -> SequenceMatcher:
    return SequenceMatcher("vehicle_dropoff", pattern)


def mark(name: str, t_ms: float) -> Mark:
    return Mark(name, ms_to_ns(t_ms))


# ------------------------------------------------------------------ shape validation


def test_a_pattern_needs_at_least_one_step() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        SequencePattern(steps=(), within_ms=1000.0)


def test_a_pattern_window_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SequencePattern(steps=(Step("a"),), within_ms=0.0)


def test_a_step_needs_a_name_and_a_non_negative_duration() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        Step("")
    with pytest.raises(ValueError, match="must not be negative"):
        Step("a", min_duration_ms=-1.0)


# ------------------------------------------------------------------ matching


def test_the_full_ordered_pattern_matches() -> None:
    m = matcher()
    assert m.advance(SUBJECT, mark("stopped", 0.0)) is None
    assert m.advance(SUBJECT, mark("stopped", 2000.0)) is None  # held, but not long enough
    assert m.advance(SUBJECT, mark("stopped", 3000.0)) is None  # step 0 completes
    assert m.advance(SUBJECT, mark("person_exit", 4000.0)) is None
    match = m.advance(SUBJECT, mark("departed", 5000.0))
    assert match is not None
    assert match.pattern_id == "vehicle_dropoff"
    assert match.started_monotonic_ns == ms_to_ns(0.0)
    assert match.elapsed_ms == pytest.approx(5000.0)


def test_a_step_requiring_duration_is_not_satisfied_by_one_instant() -> None:
    m = matcher()
    m.advance(SUBJECT, mark("stopped", 0.0))
    assert m.advance(SUBJECT, mark("person_exit", 100.0)) is None
    # Still waiting on step 0, so the later steps cannot have been consumed.
    assert m.advance(SUBJECT, mark("departed", 200.0)) is None


def test_steps_out_of_order_do_not_match() -> None:
    m = matcher()
    assert m.advance(SUBJECT, mark("departed", 0.0)) is None
    assert m.advance(SUBJECT, mark("person_exit", 100.0)) is None
    assert len(m) == 1  # progress exists, but still on step 0


def test_a_gap_tolerant_pattern_ignores_unrelated_marks() -> None:
    m = matcher()
    m.advance(SUBJECT, mark("stopped", 0.0))
    m.advance(SUBJECT, mark("stopped", 3000.0))
    m.advance(SUBJECT, mark("something_else", 3100.0))
    m.advance(SUBJECT, mark("person_exit", 3200.0))
    assert m.advance(SUBJECT, mark("departed", 3300.0)) is not None


def test_a_strict_pattern_is_broken_by_an_unrelated_mark() -> None:
    strict = SequencePattern(steps=(Step("a"), Step("b")), within_ms=10_000.0, allow_gaps=False)
    m = matcher(strict)
    m.advance(SUBJECT, mark("a", 0.0))
    m.advance(SUBJECT, mark("x", 100.0))  # breaks it
    assert m.advance(SUBJECT, mark("b", 200.0)) is None


def test_the_window_expiring_restarts_the_pattern() -> None:
    m = matcher()
    m.advance(SUBJECT, mark("stopped", 0.0))
    m.advance(SUBJECT, mark("stopped", 3000.0))
    m.advance(SUBJECT, mark("person_exit", 200_000.0))  # beyond within_ms
    assert m.advance(SUBJECT, mark("departed", 200_100.0)) is None


def test_subjects_progress_independently() -> None:
    m = matcher()
    for t in (0.0, 3000.0):
        m.advance(SUBJECT, mark("stopped", t))
    m.advance(OTHER, mark("stopped", 3000.0))
    m.advance(SUBJECT, mark("person_exit", 3100.0))
    assert m.advance(SUBJECT, mark("departed", 3200.0)) is not None
    assert m.advance(OTHER, mark("departed", 3300.0)) is None  # OTHER is still on step 0


def test_a_match_clears_that_subject_s_progress() -> None:
    m = matcher()
    for t, name in ((0.0, "stopped"), (3000.0, "stopped"), (3100.0, "person_exit")):
        m.advance(SUBJECT, mark(name, t))
    assert m.advance(SUBJECT, mark("departed", 3200.0)) is not None
    assert len(m) == 0


# ------------------------------------------------------------------ bounded state (rule 4)


def test_state_can_be_forgotten_per_subject_and_wholesale() -> None:
    m = matcher()
    m.advance(SUBJECT, mark("stopped", 0.0))
    m.advance(OTHER, mark("stopped", 0.0))
    assert len(m) == 2
    m.forget(SUBJECT)
    assert len(m) == 1
    m.reset()
    assert len(m) == 0


def test_marks_must_arrive_in_time_order() -> None:
    m = matcher()
    m.advance(SUBJECT, mark("stopped", 1000.0))
    with pytest.raises(TemporalError, match="time order"):
        m.advance(SUBJECT, mark("stopped", 500.0))


def test_an_empty_pattern_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        SequenceMatcher("", PATTERN)


# ------------------------------------------------------------------ feeding accumulation


def test_a_match_becomes_one_observation_that_accumulates_like_any_other() -> None:
    m = matcher()
    for t, name in ((0.0, "stopped"), (3000.0, "stopped"), (3100.0, "person_exit")):
        m.advance(SUBJECT, mark(name, t))
    match = m.advance(SUBJECT, mark("departed", 3200.0))
    assert match is not None
    observation = observation_from_match(
        match, camera_id="test-cam", module_id="dropoff", signal=0.9, facts={"zone_id": "dock"}
    )
    assert observation.subject == SUBJECT
    assert observation.signal == pytest.approx(0.9)
    assert observation.t_monotonic_ns == ms_to_ns(3200.0)
    assert observation.facts["sequence"] == "vehicle_dropoff"
    assert observation.facts["sequence_elapsed_ms"] == pytest.approx(3200.0)
    assert observation.facts["zone_id"] == "dock"


def test_marks_from_names_stamps_one_frame_s_conditions() -> None:
    marks = marks_from_names(["stopped", "occupied"], t_monotonic_ns=ms_to_ns(500.0))
    assert [m.name for m in marks] == ["stopped", "occupied"]
    assert {m.t_monotonic_ns for m in marks} == {ms_to_ns(500.0)}


def test_a_single_step_pattern_matches_immediately() -> None:
    m = matcher(SequencePattern(steps=(Step("boom"),), within_ms=1000.0))
    assert m.advance(SUBJECT, mark("boom", 0.0)) is not None
