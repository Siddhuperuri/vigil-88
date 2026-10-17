"""Arbitration between verified events (06 §7).

Runs after validation, before severity. Resolves relationships that validators, each looking at
one candidate, cannot see.

Deliberately a small, explicit rule set in config rather than a learned or heuristic layer: four
named rules an operator can read beat a scoring function nobody can predict.

P3 implements the two rules that the modules shipped in P3 can actually trigger — **subsume**
and **escalate** — plus the identity machinery for **merge** and **spatial coalesce**. Those two
need `FIRE`/`SMOKE` and `region:` subjects, which arrive in P6 and have no module here; wiring
them now would mean writing rules against events that cannot occur, and testing them against
fabricated ones. `docs/LIMITATIONS.md` records this.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vigil.config.schema.modules import ArbitrationConfig
from vigil.core.units import ns_to_ms
from vigil.domain.enums import EventType
from vigil.domain.event import VerifiedEvent

# A specific event type inside a broader active one is recorded on the broader event rather than
# reported separately: two incidents for one situation is a reporting failure.
SUBSUMED_BY: dict[EventType, EventType] = {
    EventType.RESTRICTED_AREA_ENTRY: EventType.INTRUSION,
}


@dataclass(frozen=True, slots=True)
class ArbitrationOutcome:
    """What arbitration decided about one event."""

    event: VerifiedEvent
    suppressed_by: str | None = None
    escalation_count: int = 0
    notes: tuple[str, ...] = ()

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
        if count >= config.escalation_min_events:
            notes.append(
                f"{count} events of this type on this camera within "
                f"{config.escalation_window_ms / 1000:.0f} s"
            )
        outcomes.append(
            ArbitrationOutcome(
                event=event,
                suppressed_by=suppressed_by,
                escalation_count=count,
                notes=tuple(notes),
            )
        )
    return tuple(outcomes)


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
