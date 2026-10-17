"""Building a candidate from an accumulator state (04 §1 `promote`)."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.support.candidates import CAMERA, SHARED_ULIDS, accumulate, ulids
from vigil.core.ids import is_valid_ulid
from vigil.core.units import ms_to_ns
from vigil.domain.enums import EventType
from vigil.domain.observation import Observation, SubjectKey
from vigil.events.promotion import involved_track_ids, involved_zone_ids, promote

PROMOTED_AT = datetime(2026, 1, 1, 2, 41, tzinfo=UTC)


def observation(subject: SubjectKey, **facts: object) -> Observation:
    return Observation(
        camera_id=CAMERA,
        module_id="intrusion",
        subject=subject,
        signal=0.8,
        t_monotonic_ns=0,
        facts=facts,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ track extraction


def test_a_track_subject_names_its_own_track() -> None:
    assert involved_track_ids(SubjectKey("track", "42"), ()) == frozenset({42})


def test_a_non_numeric_track_subject_is_survived() -> None:
    """A malformed subject must not crash promotion; it simply names no track."""
    assert involved_track_ids(SubjectKey("track", "not-a-number"), ()) == frozenset()


def test_tracks_are_read_from_facts_for_other_subject_kinds() -> None:
    """A zone-occupancy hypothesis is about whoever is standing there, and the module knows."""
    observations = (
        observation(SubjectKey("zone", "dock"), track_id=7.0),
        observation(SubjectKey("zone", "dock"), track_id=9.0),
    )
    assert involved_track_ids(SubjectKey("zone", "dock"), observations) == frozenset({7, 9})


def test_a_string_track_fact_is_parsed() -> None:
    observations = (observation(SubjectKey("zone", "dock"), track_id="11"),)
    assert involved_track_ids(SubjectKey("zone", "dock"), observations) == frozenset({11})


def test_an_unparseable_string_track_fact_is_ignored() -> None:
    observations = (observation(SubjectKey("zone", "dock"), track_id="eleven"),)
    assert involved_track_ids(SubjectKey("zone", "dock"), observations) == frozenset()


def test_a_boolean_track_fact_is_not_mistaken_for_an_id() -> None:
    """`True` is an int in Python, and track 1 is a real id: the distinction matters."""
    observations = (observation(SubjectKey("zone", "dock"), track_id=True),)
    assert involved_track_ids(SubjectKey("zone", "dock"), observations) == frozenset()


# ------------------------------------------------------------------ zone extraction


def test_a_zone_subject_names_its_own_zone() -> None:
    assert involved_zone_ids(SubjectKey("zone", "dock"), ()) == frozenset({"dock"})


def test_zones_are_read_from_facts() -> None:
    observations = (
        observation(SubjectKey("track", "1"), zone_id="yard"),
        observation(SubjectKey("track", "1"), zone_id="dock"),
    )
    assert involved_zone_ids(SubjectKey("track", "1"), observations) == frozenset({"yard", "dock"})


def test_a_non_string_zone_fact_is_ignored() -> None:
    observations = (observation(SubjectKey("track", "1"), zone_id=3.0),)
    assert involved_zone_ids(SubjectKey("track", "1"), observations) == frozenset()


def test_an_empty_zone_fact_is_ignored() -> None:
    observations = (observation(SubjectKey("track", "1"), zone_id=""),)
    assert involved_zone_ids(SubjectKey("track", "1"), observations) == frozenset()


def test_a_camera_subject_names_no_zone() -> None:
    assert involved_zone_ids(SubjectKey("camera", ""), ()) == frozenset()


# ------------------------------------------------------------------ promotion


def test_promotion_carries_the_whole_window() -> None:
    """The window is the evidence, and later the explanation an operator interrogates."""
    accumulator = accumulate(signals=(0.8,) * 13)
    now_ns = ms_to_ns(1200.0)
    candidate = promote(
        state=accumulator.state_at(now_ns),
        event_type=EventType.INTRUSION,
        observations=accumulator.window(),
        promoted_monotonic_ns=now_ns,
        promoted_wall_utc=PROMOTED_AT,
        ulids=SHARED_ULIDS,
    )
    assert len(candidate.observations) == 13
    assert candidate.accumulator.observation_count == 13
    assert candidate.zone_ids == frozenset({"yard"})
    assert candidate.involved_track_ids == frozenset({1})
    assert candidate.promoted_monotonic_ns == now_ns
    assert candidate.promoted_wall_utc == PROMOTED_AT


def test_a_candidate_gets_a_sortable_ulid() -> None:
    accumulator = accumulate()
    candidate = promote(
        state=accumulator.state,
        event_type=EventType.INTRUSION,
        observations=accumulator.window(),
        promoted_monotonic_ns=0,
        promoted_wall_utc=PROMOTED_AT,
        ulids=SHARED_ULIDS,
    )
    assert is_valid_ulid(candidate.candidate_id)


def test_two_candidates_from_one_factory_get_distinct_ids() -> None:
    factory = ulids(1)
    accumulator = accumulate()
    made = [
        promote(
            state=accumulator.state,
            event_type=EventType.INTRUSION,
            observations=accumulator.window(),
            promoted_monotonic_ns=0,
            promoted_wall_utc=PROMOTED_AT,
            ulids=factory,
        )
        for _ in range(2)
    ]
    assert made[0].candidate_id != made[1].candidate_id
    assert made[0].candidate_id < made[1].candidate_id  # ULIDs sort by creation order


def test_promotion_preserves_the_module_and_camera_from_the_state() -> None:
    accumulator = accumulate(camera_id="cam-07", module_id="intrusion")
    candidate = promote(
        state=accumulator.state,
        event_type=EventType.INTRUSION,
        observations=accumulator.window(),
        promoted_monotonic_ns=0,
        promoted_wall_utc=PROMOTED_AT,
        ulids=SHARED_ULIDS,
    )
    assert candidate.camera_id == "cam-07"
    assert candidate.module_id == "intrusion"
    assert candidate.event_type is EventType.INTRUSION


def test_promotion_of_an_empty_window_is_still_well_formed() -> None:
    """Defensive: the accumulator bounds its window, so a long-running one may have dropped
    every retained observation. The candidate must still be constructible."""
    accumulator = accumulate(signals=(0.8,) * 5)
    candidate = promote(
        state=accumulator.state,
        event_type=EventType.INTRUSION,
        observations=(),
        promoted_monotonic_ns=0,
        promoted_wall_utc=PROMOTED_AT,
        ulids=SHARED_ULIDS,
    )
    assert candidate.observations == ()
    assert candidate.zone_ids == frozenset()
    assert candidate.involved_track_ids == frozenset({1})  # still known from the subject
