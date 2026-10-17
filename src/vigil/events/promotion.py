"""Building a `CandidateEvent` from an accumulator that has crossed activation (04 §1 `promote`).

This is the only place a candidate comes into existence. It takes pure domain values — a state,
the window that produced it, and its trace — rather than an accumulator object, so `events/`
needs no import of `temporal/`: sibling engines must not depend on each other, and `pipeline/`
wires them (01 §5).

A candidate is not an incident. It is the system saying "I believe X is happening", which
verification may still refuse.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from vigil.core.ids import UlidFactory
from vigil.domain.enums import EventType
from vigil.domain.event import AccumulatorState, CandidateEvent
from vigil.domain.observation import Observation, SubjectKey

ZONE_FACT_KEYS = ("zone_id", "zone")
TRACK_FACT_KEYS = ("track_id",)


def involved_track_ids(subject: SubjectKey, observations: Sequence[Observation]) -> frozenset[int]:
    """Which tracks this candidate is about.

    A `track:` subject names one directly. Other subject kinds may still involve tracks — a
    zone-occupancy hypothesis is about whoever is standing there — and those are read from the
    observation facts, because the module is what knows.
    """
    found: set[int] = set()
    if subject.kind == "track":
        track_id = _as_int(subject.value)
        if track_id is not None:
            found.add(track_id)
    for obs in observations:
        for key in TRACK_FACT_KEYS:
            value = obs.facts.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                found.add(int(value))
            elif isinstance(value, str):
                parsed = _as_int(value)
                if parsed is not None:
                    found.add(parsed)
    return frozenset(found)


def involved_zone_ids(subject: SubjectKey, observations: Sequence[Observation]) -> frozenset[str]:
    """Zones this candidate implicates, from the subject and from the facts the module recorded."""
    found: set[str] = set()
    if subject.kind == "zone" and subject.value:
        found.add(subject.value)
    for obs in observations:
        for key in ZONE_FACT_KEYS:
            value = obs.facts.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return frozenset(found)


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def promote(
    *,
    state: AccumulatorState,
    event_type: EventType,
    observations: Sequence[Observation],
    promoted_monotonic_ns: int,
    promoted_wall_utc: datetime,
    ulids: UlidFactory,
) -> CandidateEvent:
    """Create the candidate. The window is carried whole: it is the evidence, and later the
    explanation an operator interrogates (01 §7.5)."""
    window = tuple(observations)
    return CandidateEvent(
        candidate_id=ulids.new(),
        camera_id=state.camera_id,
        module_id=state.module_id,
        event_type=event_type,
        subject=state.subject,
        accumulator=state,
        observations=window,
        involved_track_ids=involved_track_ids(state.subject, window),
        zone_ids=involved_zone_ids(state.subject, window),
        promoted_monotonic_ns=promoted_monotonic_ns,
        promoted_wall_utc=promoted_wall_utc,
    )
