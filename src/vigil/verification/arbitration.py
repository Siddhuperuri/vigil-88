"""Arbitration between verified events (06 §7).

Runs after validation, before severity. Resolves relationships that validators, each looking at
one candidate, cannot see.

Deliberately a small, explicit rule set in config rather than a learned or heuristic layer: four
named rules an operator can read beat a scoring function nobody can predict.

Three of the four rules of 06 §7 are implemented: **subsume**, **escalate** and **merge**.
**Spatial coalesce** is not — it needs `region:` subjects, which no module in this build emits
(see `merge` below and `docs/LIMITATIONS.md`).

`merge` was held back in P3 for want of `FIRE`/`SMOKE`. P6 ships both as declarations with no
detector (ADR 0015), so neither event type has a producer here either — but the rule is pure
logic over two verified events and is reachable by a third-party module through the
`vigil88.modules` entry-point group, so it is implemented and unit-tested rather than deferred
again. What it has is no *production* exercise, which LIMITATIONS states.

A suppression decided here is **acted on**: `pipeline/scoring.py` keeps the suppressed event out
of `scored`, so no incident is created for it, and rewrites its `CandidateRecord` to
`CandidateOutcome.SUPPRESSED` with the reason. Before P6 the decision was computed and only
logged, which made subsume a rule in name only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from vigil.config.schema.modules import ArbitrationConfig
from vigil.core.units import ns_to_ms
from vigil.domain.enums import CandidateOutcome, EventType
from vigil.domain.event import CandidateRecord, VerifiedEvent

# A specific event type inside a broader active one is recorded on the broader event rather than
# reported separately: two incidents for one situation is a reporting failure.
SUBSUMED_BY: dict[EventType, EventType] = {
    EventType.RESTRICTED_AREA_ENTRY: EventType.INTRUSION,
}

# 06 §7 merge: the contributing type on the left is folded into the dominant type on the right.
# One direction only, and asserted so by a test: a symmetric table would let two events suppress
# each other and leave nothing reported at all.
MERGED_INTO: dict[EventType, EventType] = {
    EventType.SMOKE: EventType.FIRE,
}


@dataclass(frozen=True, slots=True)
class ArbitrationOutcome:
    """What arbitration decided about one event."""

    event: VerifiedEvent
    suppressed_by: str | None = None
    escalation_count: int = 0
    notes: tuple[str, ...] = ()
    # Candidate ids this event absorbed. The dominant half of a merge names its contributors so
    # the smoke that was folded into a fire is recorded rather than discarded (06 §7).
    merged_from: tuple[str, ...] = ()

    @property
    def suppressed(self) -> bool:
        return self.suppressed_by is not None


def arbitrate(
    events: Sequence[VerifiedEvent],
    *,
    config: ArbitrationConfig,
    now_monotonic_ns: int,
    active: Sequence[VerifiedEvent] = (),
) -> tuple[ArbitrationOutcome, ...]:
    """Decide each event's standing against the others and against what is already active.

    Deterministic: events are considered in a sorted order, so the same input always yields the
    same decisions.
    """
    ordered = sorted(
        events,
        key=lambda e: (
            e.candidate.camera_id,
            e.candidate.event_type.value,
            str(e.candidate.subject),
            e.candidate.candidate_id,
        ),
    )
    pool = (*active, *ordered)
    outcomes: list[ArbitrationOutcome] = []
    for event in ordered:
        suppressed_by = _subsuming_parent(event, pool)
        count = _escalation_count(event, pool, config=config, now_ns=now_monotonic_ns)
        notes: list[str] = []
        if suppressed_by is not None:
            notes.append(
                f"recorded as a transition on {suppressed_by}: the broader event already "
                "describes this situation"
            )
        merged_into = None if suppressed_by is not None else _merge_target(event, pool, config)
        if merged_into is not None:
            suppressed_by = merged_into
            notes.append(
                f"merged into {merged_into} as a contributing signal: "
                f"{event.candidate.event_type.value} and "
                f"{MERGED_INTO[event.candidate.event_type].value} on the same subject within "
                f"{config.merge_window_ms / 1000:.0f} s describe one situation, and two "
                "incidents for one situation is a reporting failure"
            )
        if count >= config.escalation_min_events:
            notes.append(
                f"{count} events of this type on this camera within "
                f"{config.escalation_window_ms / 1000:.0f} s"
            )
        absorbed = _absorbed_by(event, pool, config)
        if absorbed:
            notes.append(
                "carries contributing signals: " + ", ".join(sorted(absorbed)),
            )
        outcomes.append(
            ArbitrationOutcome(
                event=event,
                suppressed_by=suppressed_by,
                escalation_count=count,
                notes=tuple(notes),
                merged_from=tuple(sorted(absorbed)),
            )
        )
    return tuple(outcomes)


def suppressed_record(record: CandidateRecord, outcome: ArbitrationOutcome) -> CandidateRecord:
    """Rewrite a VERIFIED record as SUPPRESSED, carrying arbitration's reason.

    A candidate that arbitration folded into another event is neither an incident nor a
    rejection: it survived every validator and was then recorded somewhere else. 05 §5 already
    has `CandidateOutcome.SUPPRESSED` for exactly this, and leaving the record VERIFIED with no
    incident behind it would make the candidate table disagree with the incident table.
    """
    if outcome.suppressed_by is None:
        return record
    return replace(
        record,
        outcome=CandidateOutcome.SUPPRESSED,
        rejection_reason="; ".join(outcome.notes) or f"suppressed by {outcome.suppressed_by}",
    )


def _subsuming_parent(event: VerifiedEvent, pool: Sequence[VerifiedEvent]) -> str | None:
    """A specific event inside a broader one for the same subject is subsumed by it."""
    broader = SUBSUMED_BY.get(event.candidate.event_type)
    if broader is None:
        return None
    for other in pool:
        if other.candidate.candidate_id == event.candidate.candidate_id:
            continue
        if (
            other.candidate.camera_id == event.candidate.camera_id
            and other.candidate.event_type is broader
            and other.candidate.subject == event.candidate.subject
        ):
            return other.candidate.candidate_id
    return None


def _merge_target(
    event: VerifiedEvent, pool: Sequence[VerifiedEvent], config: ArbitrationConfig
) -> str | None:
    """The candidate id of the dominant event this one is folded into, if there is one.

    **Overlap is subject equality.** 06 §7 says "overlapping regions", and the only mechanical
    reading available is the subject, because that is what the accumulator was keyed by: two
    `region:` observations of the same grid cell share a subject, as do two `zone:` or two
    `camera:` ones. *Adjacent* cells are the **spatial coalesce** rule's job, which is not built
    — so without it, merge joins events on the identical subject and nothing wider. Guessing at
    overlap from `zone_ids` across different subject kinds would merge a fire at one end of a
    yard with steam at the other, and a fabricated merge is worse than two incidents.
    """
    dominant = MERGED_INTO.get(event.candidate.event_type)
    if dominant is None:
        return None
    mine = event.candidate
    for other in pool:
        theirs = other.candidate
        if theirs.candidate_id == mine.candidate_id:
            continue
        if (
            theirs.camera_id == mine.camera_id
            and theirs.event_type is dominant
            and theirs.subject == mine.subject
            and abs(ns_to_ms(theirs.promoted_monotonic_ns - mine.promoted_monotonic_ns))
            <= config.merge_window_ms
        ):
            return theirs.candidate_id
    return None


def _absorbed_by(
    event: VerifiedEvent, pool: Sequence[VerifiedEvent], config: ArbitrationConfig
) -> frozenset[str]:
    """Candidate ids that merge into this one, so the dominant event names its contributors."""
    return frozenset(
        other.candidate.candidate_id
        for other in pool
        if other.candidate.candidate_id != event.candidate.candidate_id
        and _merge_target(other, (event,), config) == event.candidate.candidate_id
    )


def _escalation_count(
    event: VerifiedEvent,
    pool: Sequence[VerifiedEvent],
    *,
    config: ArbitrationConfig,
    now_ns: int,
) -> int:
    """Related events of the same type on one camera inside the escalation window.

    Feeds the severity engine's `escalation` factor. It counts rather than creating a
    `CROWD_ANOMALY` candidate: that type has no module in P3, and promoting a candidate from
    arbitration would be a second promotion path, which the architecture forbids.
    """
    return sum(
        1
        for other in pool
        if other.candidate.camera_id == event.candidate.camera_id
        and other.candidate.event_type is event.candidate.event_type
        and ns_to_ms(now_ns - other.candidate.promoted_monotonic_ns) <= config.escalation_window_ms
    )
