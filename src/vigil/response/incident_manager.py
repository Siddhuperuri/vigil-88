"""Turning verified events into incidents, and the lifecycle they then follow (06 §9, §10).

Three guarantees, each of which is a defect if it fails.

**One situation is one incident.** The dedup key is `(camera, event_type, subject)` -- the
identity of a situation, the same tuple the suppression ledger uses -- and a verified event for
a key that already has an open incident *extends* it. A person standing in a restricted zone for
a minute is one incident with a growing span, not sixty incidents. The check is a single indexed
lookup, so it stays cheap on the response path.

**Duplicate delivery is harmless.** The response worker may retry after a transient storage
failure, and the same decided event may arrive twice. Creation is idempotent on the incident id,
and `last_observed` only ever moves forward, so a replayed older event cannot rewind a span.

**Terminal states are terminal.** A resolved incident is never reopened; a recurrence is a new
incident. Reopening would destroy the meaning of `duration_ms` and make historical analysis
incoherent (06 §9). The transition table below is the only place a status may change, and every
change writes an `IncidentTransition` with its actor and reason -- the lifecycle is an audit
trail, not a mutable field.

This engine fails **open** (01 §6): if evidence or alerting fails the incident is still
persisted, marked partial, and the gap is visible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from vigil.config.settings import Settings
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.ids import UlidFactory, display_ref
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import NS_PER_MS
from vigil.domain.detection import ModelDescriptor
from vigil.domain.enums import EventType, IncidentStatus, Severity
from vigil.domain.event import AccumulatorSample, VerifiedEvent
from vigil.domain.incident import (
    Actor,
    Incident,
    IncidentExplanation,
    IncidentTransition,
    SeverityAssessment,
    SystemSnapshot,
)
from vigil.domain.observation import SubjectKey
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.repositories.incidents import (
    IncidentRepository,
    OpenIncident,
)
from vigil.response.evidence import EvidenceStatus
from vigil.response.explanation import build_explanation

# 06 §9. Anything not listed is refused: a status change the architecture does not define is a
# bug in the caller, and silently allowing it would corrupt the audit trail.
ALLOWED_TRANSITIONS: Mapping[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.CANDIDATE: frozenset(
        {
            IncidentStatus.CONFIRMED,
            IncidentStatus.DISMISSED,
            IncidentStatus.SUPPRESSED,
            IncidentStatus.EXPIRED,
        }
    ),
    IncidentStatus.CONFIRMED: frozenset(
        {
            IncidentStatus.ACTIVE,
            IncidentStatus.RESOLVING,
            IncidentStatus.DISMISSED,
            IncidentStatus.SUPPRESSED,
        }
    ),
    IncidentStatus.ACTIVE: frozenset(
        {IncidentStatus.RESOLVING, IncidentStatus.DISMISSED, IncidentStatus.SUPPRESSED}
    ),
    # The one non-monotonic edge: new supporting evidence returns a releasing incident to ACTIVE.
    # The hysteresis gap (04 §6.3) exists to make this rare.
    IncidentStatus.RESOLVING: frozenset(
        {IncidentStatus.RESOLVED, IncidentStatus.ACTIVE, IncidentStatus.DISMISSED}
    ),
    IncidentStatus.RESOLVED: frozenset(),
    IncidentStatus.DISMISSED: frozenset(),
    IncidentStatus.EXPIRED: frozenset(),
    IncidentStatus.SUPPRESSED: frozenset(),
}

TERMINAL: frozenset[IncidentStatus] = frozenset(
    s for s, allowed in ALLOWED_TRANSITIONS.items() if not allowed
)


def dedup_key_text(camera_id: str, event_type: EventType, subject: SubjectKey) -> str:
    """The stored form of a situation's identity. One definition, used by writes and reads."""
    return f"{camera_id}|{event_type.value}|{subject}"


@dataclass(frozen=True, slots=True)
class IncidentContext:
    """What an incident needs that the event itself cannot carry.

    `location`, `model` and `system` are **copied onto** the incident rather than referenced:
    an incident is an immutable historical fact and must not change meaning because a camera was
    renamed or a model was swapped afterwards (05 §4).
    """

    tracker_epoch: int
    module_version: str
    location: str | None
    model: ModelDescriptor
    system: SystemSnapshot
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IncidentOutcome:
    incident_id: str
    display_ref: str
    created: bool
    extended: bool
    status: IncidentStatus
    severity: Severity
    camera_id: str
    event_type: EventType
    confirmed_wall_utc: datetime
    # The explanation that was persisted. Handed back so evidence capture writes the *same* one
    # rather than rebuilding it from config that may already have moved on.
    explanation: IncidentExplanation | None = None

    @property
    def is_new(self) -> bool:
        return self.created


class TransitionRefused(PersistenceError):
    """A status change the lifecycle does not allow. Raised before anything is written."""


class IncidentManager:
    """One per process. Holds no incident state: the database is the only truth."""

    __slots__ = ("_clock", "_incidents", "_log", "_metrics", "_settings", "_ulids")

    def __init__(
        self,
        *,
        incidents: IncidentRepository,
        settings: Settings,
        clock: Clock,
        ulids: UlidFactory,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._incidents = incidents
        self._settings = settings
        self._clock = clock
        self._ulids = ulids
        self._log = logger.bind(component="incidents")
        self._metrics = metrics

    # ---------------------------------------------------------------- creation

    def record(
        self,
        event: VerifiedEvent,
        assessment: SeverityAssessment,
        context: IncidentContext,
        *,
        accumulator_trace: Sequence[AccumulatorSample] = (),
    ) -> IncidentOutcome:
        """Create an incident for this event, or extend the open one for the same situation."""
        candidate = event.candidate
        key = dedup_key_text(candidate.camera_id, candidate.event_type, candidate.subject)
        existing = self._incidents.open_for(key)
        if existing is not None:
            return self._extend(existing, event, assessment)
        return self._create(event, assessment, context, key, accumulator_trace)

    def _extend(
        self,
        existing: OpenIncident,
        event: VerifiedEvent,
        assessment: SeverityAssessment,
    ) -> IncidentOutcome:
        candidate = event.candidate
        last_observed = self._wall_at(event, candidate.accumulator.last_observation_ns)
        duration_ms = (last_observed - existing.confirmed_wall_utc).total_seconds() * 1000.0
        self._incidents.extend(
            existing.incident_id,
            last_observed_wall_utc=last_observed,
            duration_ms=max(duration_ms, 0.0),
            confidence_ratio=event.confidence_ratio,
            severity_score=assessment.score,
            severity=int(assessment.severity),
            now_wall_utc=self._clock.wall_utc(),
        )
        # A releasing incident that receives new supporting evidence returns to ACTIVE (06 §9).
        status = existing.status
        if status is IncidentStatus.RESOLVING:
            self.transition(
                existing.incident_id,
                IncidentStatus.ACTIVE,
                actor="system",
                reason="new supporting evidence arrived while the incident was releasing",
            )
            status = IncidentStatus.ACTIVE
        self._metrics.counter(
            "vigil_incidents_extended_total",
            camera=candidate.camera_id,
            event_type=candidate.event_type.value,
        ).inc()
        self._log.info(
            "incident_extended",
            incident_id=existing.incident_id,
            display_ref=existing.display_ref,
            candidate_id=candidate.candidate_id,
            duration_ms=round(duration_ms, 1),
        )
        return IncidentOutcome(
            incident_id=existing.incident_id,
            display_ref=existing.display_ref,
            created=False,
            extended=True,
            status=status,
            severity=assessment.severity,
            camera_id=candidate.camera_id,
            event_type=candidate.event_type,
            confirmed_wall_utc=existing.confirmed_wall_utc,
        )

    def _create(
        self,
        event: VerifiedEvent,
        assessment: SeverityAssessment,
        context: IncidentContext,
        key: str,
        accumulator_trace: Sequence[AccumulatorSample],
    ) -> IncidentOutcome:
        candidate = event.candidate
        incident_id = self._ulids.new()
        confirmed = candidate.promoted_wall_utc
        accumulator = candidate.accumulator
        first_observed = self._wall_at(event, accumulator.first_observation_ns)
        last_observed = self._wall_at(event, accumulator.last_observation_ns)
        reference = display_ref(incident_id, confirmed)

        # Born CANDIDATE and immediately CONFIRMED: the architecture's state machine has both,
        # and the audit trail records both even though the two happen in one transaction --
        # promotion-and-verification is one step, scoring-and-persisting the next (06 §9).
        transitions = (
            IncidentTransition(
                from_status=None,
                to_status=IncidentStatus.CANDIDATE,
                at_wall_utc=confirmed,
                actor="system",
                reason=(
                    f"promoted by {candidate.module_id} and passed {len(event.verdicts)} "
                    "validator(s)"
                ),
            ),
            IncidentTransition(
                from_status=IncidentStatus.CANDIDATE,
                to_status=IncidentStatus.CONFIRMED,
                at_wall_utc=confirmed,
                actor="system",
                reason=(
                    f"severity {assessment.severity.name} ({assessment.score:.1f}) scored and "
                    "persisted"
                ),
            ),
        )
        explanation = build_explanation(
            event=event,
            assessment=assessment,
            settings=self._settings,
            accumulator_trace=accumulator_trace,
            caveats=context.caveats,
        )
        incident = Incident(
            incident_id=incident_id,
            display_ref=reference,
            camera_id=candidate.camera_id,
            event_type=candidate.event_type,
            module_id=candidate.module_id,
            module_version=context.module_version,
            status=IncidentStatus.CONFIRMED,
            severity=assessment.severity,
            severity_score=assessment.score,
            confidence_ratio=event.confidence_ratio,
            zone_ids=candidate.zone_ids,
            location=context.location,
            first_observed_wall_utc=first_observed,
            confirmed_wall_utc=confirmed,
            last_observed_wall_utc=max(last_observed, first_observed),
            resolved_wall_utc=None,
            duration_ms=None,
            involved_track_ids=candidate.involved_track_ids,
            tracker_epoch=context.tracker_epoch,
            evidence_ids=(),
            explanation=explanation,
            transitions=transitions,
            model_metadata=context.model,
            system_metadata=context.system,
            operator_note=None,
        )
        created = self._incidents.insert(
            incident,
            dedup_key=key,
            candidate_id=candidate.candidate_id,
            evidence_status=EvidenceStatus.PENDING.value,
            now_wall_utc=self._clock.wall_utc(),
        )
        if created:
            self._metrics.counter(
                "vigil_incidents_created_total",
                camera=candidate.camera_id,
                event_type=candidate.event_type.value,
                severity=assessment.severity.name.lower(),
            ).inc()
            self._metrics.histogram(
                "vigil_incident_detection_latency_ms", camera=candidate.camera_id
            ).observe(incident.detection_latency_ms)
            self._log.info(
                "incident_created",
                incident_id=incident_id,
                display_ref=reference,
                camera_id=candidate.camera_id,
                event_type=candidate.event_type.value,
                severity=assessment.severity.name,
                severity_score=round(assessment.score, 2),
                confidence_ratio=round(event.confidence_ratio, 4),
                detection_latency_ms=round(incident.detection_latency_ms, 1),
                candidate_id=candidate.candidate_id,
            )
        else:
            # Only reachable on a duplicate delivery of the same decision, which is exactly
            # what the idempotent insert exists for.
            self._log.info("incident_already_recorded", incident_id=incident_id)
        return IncidentOutcome(
            incident_id=incident_id,
            display_ref=reference,
            created=created,
            extended=False,
            status=IncidentStatus.CONFIRMED,
            severity=assessment.severity,
            camera_id=candidate.camera_id,
            event_type=candidate.event_type,
            confirmed_wall_utc=confirmed,
            explanation=explanation,
        )

    def _wall_at(self, event: VerifiedEvent, t_monotonic_ns: int) -> datetime:
        """Convert a monotonic instant to wall time using the candidate's own pair of stamps.

        Both stamps come from the same frame, so the conversion needs no clock read and stays
        correct across an NTP correction -- which is the reason domain types carry the pair
        (05 §1 rule 4).
        """
        candidate = event.candidate
        delta_ms = (t_monotonic_ns - candidate.promoted_monotonic_ns) / NS_PER_MS
        return candidate.promoted_wall_utc + timedelta(milliseconds=delta_ms)

    def extend_ongoing(
        self,
        camera_id: str,
        event_type: EventType,
        subject: SubjectKey,
        *,
        last_observed_wall_utc: datetime,
        confidence_ratio: float,
    ) -> str | None:
        """Grow an open incident's span because evidence is still arriving (06 §9).

        Severity is deliberately *not* recomputed: nothing was re-scored, so claiming a new
        assessment would invent one. `extend` keeps the highest severity seen, so passing the
        current values through leaves it alone.
        """
        key = dedup_key_text(camera_id, event_type, subject)
        open_incident = self._incidents.open_for(key)
        if open_incident is None:
            return None
        duration_ms = (
            last_observed_wall_utc - open_incident.confirmed_wall_utc
        ).total_seconds() * 1000.0
        self._incidents.extend(
            open_incident.incident_id,
            last_observed_wall_utc=last_observed_wall_utc,
            duration_ms=max(duration_ms, 0.0),
            confidence_ratio=confidence_ratio,
            severity_score=open_incident.severity_score,
            severity=0,  # MAX() in the UPDATE leaves the stored band untouched
            now_wall_utc=self._clock.wall_utc(),
        )
        self._metrics.counter(
            "vigil_incidents_extended_total", camera=camera_id, event_type=event_type.value
        ).inc()
        return open_incident.incident_id

    # ---------------------------------------------------------------- lifecycle

    def transition(
        self,
        incident_id: str,
        to_status: IncidentStatus,
        *,
        actor: Actor,
        reason: str,
        at_wall_utc: datetime | None = None,
    ) -> IncidentTransition:
        """Apply one lifecycle change. Refuses anything the state machine does not allow."""
        incident = self._incidents.lifecycle(incident_id)
        if incident is None:
            raise PersistenceError(f"no incident {incident_id}")
        current = incident.status
        if to_status not in ALLOWED_TRANSITIONS[current]:
            allowed = ", ".join(sorted(s.value for s in ALLOWED_TRANSITIONS[current])) or "(none)"
            raise TransitionRefused(
                f"an incident in {current.value} cannot move to {to_status.value}",
                context={"incident_id": incident_id, "allowed": allowed},
            )
        at = at_wall_utc or self._clock.wall_utc()
        transition = IncidentTransition(
            from_status=current, to_status=to_status, at_wall_utc=at, actor=actor, reason=reason
        )
        # A terminal transition must never record a `resolved` that precedes `confirmed`: the
        # `Incident` constructor refuses that pair, so such a row would be *unreadable* -- the
        # incident would vanish from the console rather than merely look odd. A wall clock can
        # legitimately read backwards (an NTP correction between the two writes), so this is
        # clamped rather than trusted.
        closing = to_status in TERMINAL
        resolved_at = max(at, incident.confirmed_wall_utc) if closing else None
        duration = (
            max((at - incident.confirmed_wall_utc).total_seconds() * 1000.0, 0.0)
            if closing
            else None
        )
        self._incidents.transition(
            incident_id,
            transition,
            resolved_wall_utc=resolved_at,
            duration_ms=None if duration is None else max(duration, 0.0),
        )
        self._metrics.counter(
            "vigil_incident_transitions_total", to_status=to_status.value, actor=actor
        ).inc()
        self._log.info(
            "incident_transition",
            incident_id=incident_id,
            display_ref=incident.display_ref,
            from_status=current.value,
            to_status=to_status.value,
            actor=actor,
            reason=reason,
        )
        return transition

    def release(
        self, camera_id: str, event_type: EventType, subject: SubjectKey, *, sustain_ms: float
    ) -> str | None:
        """The accumulator says this situation is over. Walks RESOLVING then RESOLVED.

        Both transitions are recorded because the architecture defines both, and the audit trail
        should show that the release was sustained rather than instantaneous.
        """
        key = dedup_key_text(camera_id, event_type, subject)
        open_incident = self._incidents.open_for(key)
        if open_incident is None:
            return None
        incident_id = open_incident.incident_id
        current = open_incident.status
        if current in (IncidentStatus.CONFIRMED, IncidentStatus.ACTIVE):
            self.transition(
                incident_id,
                IncidentStatus.RESOLVING,
                actor="system",
                reason="accumulated evidence fell to the release threshold",
            )
            current = IncidentStatus.RESOLVING
        if current is IncidentStatus.RESOLVING:
            self.transition(
                incident_id,
                IncidentStatus.RESOLVED,
                actor="system",
                reason=f"release sustained for {sustain_ms:.0f} ms",
            )
        return incident_id

    def set_evidence_status(self, incident_id: str, status: EvidenceStatus) -> bool:
        """Evidence completeness is system state, not an operator action."""
        return self._incidents.set_evidence_status(incident_id, status.value)
