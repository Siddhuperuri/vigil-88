"""Operator actions on an incident (06 §10).

Separated from `IncidentManager` because the two have different callers and different rules. The
manager is driven by the pipeline and makes *system* transitions; everything here is driven by a
person through the API, and every one of these records `actor="operator"` in the audit trail.

06 §10's framing is the design constraint: **there are no decorative controls**. Each action has
a real effect, and `can()` exists so the API can offer one only when the lifecycle actually
permits it -- a control that is visible and enabled does what it says, or it is disabled and
says why (08 §8). The alternative, enabled-then-error, trains an operator to distrust the whole
interface.

What each one is really for:

  * **Acknowledge** stops alert re-notification and records who took it.
  * **Dismiss** is the most valuable state in the system for tuning, because the dismissal rate
    per module version is the only honest measure of whether suppression works (06 §9).
  * **Annotate** also exempts the incident from automatic retention deletion (05 §8): an
    annotated incident is a human artifact.
  * **Escalate** raises the band by one and **keeps the original score**, because the score is
    the arithmetic the severity engine produced and overwriting it would stop the explanation's
    factors summing to it. The override lives in the transition reason instead.
"""

from __future__ import annotations

from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.enums import IncidentStatus, Severity
from vigil.domain.incident import IncidentTransition
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.response.incident_manager import (
    ALLOWED_TRANSITIONS,
    IncidentManager,
    TransitionRefused,
)

MAX_NOTE_LENGTH = 2000
ACTIONS = ("acknowledge", "dismiss", "annotate", "escalate")


class OperatorActions:
    """The operator-facing half of incident management. One per process."""

    __slots__ = ("_clock", "_incidents", "_log", "_manager", "_metrics")

    def __init__(
        self,
        *,
        manager: IncidentManager,
        incidents: IncidentRepository,
        clock: Clock,
        logger: LoggerLike,
        metrics: MetricsRegistry,
    ) -> None:
        self._manager = manager
        self._incidents = incidents
        self._clock = clock
        self._log = logger.bind(component="operator")
        self._metrics = metrics

    # ---------------------------------------------------------------- transitions

    def acknowledge(self, incident_id: str, *, note: str | None = None) -> IncidentTransition:
        """CONFIRMED -> ACTIVE."""
        return self._manager.transition(
            incident_id,
            IncidentStatus.ACTIVE,
            actor="operator",
            reason=_clean(note) or "acknowledged by the operator",
        )

    def dismiss(self, incident_id: str, *, reason: str | None = None) -> IncidentTransition:
        """Terminal DISMISSED, and the measured false-positive signal."""
        transition = self._manager.transition(
            incident_id,
            IncidentStatus.DISMISSED,
            actor="operator",
            reason=_clean(reason) or "dismissed as a false positive",
        )
        self._metrics.counter("vigil_incidents_dismissed_total").inc()
        return transition

    # ---------------------------------------------------------------- annotation

    def annotate(self, incident_id: str, note: str | None) -> bool:
        """Attach or clear an operator note. Bounded: a note is a sentence, not a file."""
        cleaned = _clean(note)
        if cleaned is not None and len(cleaned) > MAX_NOTE_LENGTH:
            raise PersistenceError(
                f"an operator note is limited to {MAX_NOTE_LENGTH} characters",
                context={"length": len(cleaned)},
            )
        updated = self._incidents.set_operator_note(
            incident_id, cleaned, now_wall_utc=self._clock.wall_utc()
        )
        if updated:
            self._log.info("incident_annotated", incident_id=incident_id, cleared=cleaned is None)
        return updated

    # ---------------------------------------------------------------- escalation

    def escalate(self, incident_id: str, *, reason: str | None = None) -> Severity:
        """Raise the band by one. The score is left exactly as the severity engine computed it."""
        lifecycle = self._incidents.lifecycle(incident_id)
        if lifecycle is None:
            raise PersistenceError(f"no incident {incident_id}")
        current = Severity(lifecycle.severity)
        if current is Severity.CRITICAL:
            raise TransitionRefused(
                "an incident is already at CRITICAL and cannot be escalated further",
                context={"incident_id": incident_id},
            )
        raised = Severity(lifecycle.severity + 1)
        self._incidents.extend(
            incident_id,
            last_observed_wall_utc=lifecycle.last_observed_wall_utc,
            duration_ms=lifecycle.duration_ms or 0.0,
            confidence_ratio=lifecycle.confidence_ratio,
            severity_score=lifecycle.severity_score,
            severity=int(raised),
            now_wall_utc=self._clock.wall_utc(),
        )
        self._metrics.counter("vigil_incidents_escalated_total").inc()
        self._log.info(
            "incident_escalated",
            incident_id=incident_id,
            from_severity=current.name,
            to_severity=raised.name,
            reason=_clean(reason) or "escalated by the operator",
            original_score=round(lifecycle.severity_score, 2),
        )
        return raised

    # ---------------------------------------------------------------- what is offered

    def available_transitions(self, incident_id: str) -> tuple[IncidentStatus, ...]:
        """What this incident can actually do next, so the UI can disable the rest (08 §8)."""
        lifecycle = self._incidents.lifecycle(incident_id)
        if lifecycle is None:
            return ()
        return tuple(sorted(ALLOWED_TRANSITIONS[lifecycle.status], key=lambda s: s.value))

    def can(self, incident_id: str, action: str) -> tuple[bool, str | None]:
        """Whether one named action is permitted now, and the reason when it is not."""
        lifecycle = self._incidents.lifecycle(incident_id)
        if lifecycle is None:
            return (False, "no such incident")
        state = lifecycle.status.value
        allowed = ALLOWED_TRANSITIONS[lifecycle.status]
        if action == "acknowledge":
            ok = IncidentStatus.ACTIVE in allowed
            return (ok, None if ok else f"an incident in {state} cannot be acknowledged")
        if action == "dismiss":
            ok = IncidentStatus.DISMISSED in allowed
            return (ok, None if ok else f"an incident in {state} is already closed")
        if action == "escalate":
            at_top = Severity(lifecycle.severity) is Severity.CRITICAL
            return (not at_top, "already at CRITICAL" if at_top else None)
        if action == "annotate":
            # Always allowed, including on a closed incident: a note written during review is
            # often the most useful thing on the record.
            return (True, None)
        return (False, f"unknown action {action!r}")

    def permissions(self, incident_id: str) -> dict[str, tuple[bool, str | None]]:
        """Every action's standing in one call, which is what the detail view renders."""
        return {action: self.can(incident_id, action) for action in ACTIONS}


def _clean(text: str | None) -> str | None:
    """Strip, and reject whitespace-only input, so a reason is never an empty audit entry."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None
