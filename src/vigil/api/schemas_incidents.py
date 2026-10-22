"""Wire models for incidents, evidence, candidates and mutes (08 §11).

Separate from the domain types on purpose (02 §2): the wire format changes for client reasons,
the domain for engine reasons. The translation is explicit and boring, which is what keeps a
rename in one from silently breaking the other.

Two shapes, deliberately different:

  * `IncidentSummaryOut` is what a list row needs -- and nothing more. Loading every
    observation of every incident to render twelve rows would make the desk slow at exactly the
    moment it matters.
  * `IncidentDetailOut` carries the explanation, the transitions, the evidence manifest and
     which actions are currently permitted, because that is one screen and one request.

`permissions` is the anti-decorative-control mechanism (08 §8): the console disables what it
cannot do and says why, rather than offering a button that returns an error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from vigil.domain.evidence import Evidence
from vigil.domain.incident import Incident, IncidentTransition
from vigil.persistence.repositories.events import CandidateRow, HealthEvent
from vigil.persistence.repositories.mutes import MuteRecord


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class TransitionOut(_Out):
    from_status: str | None
    to_status: str
    at_utc: str
    actor: str
    reason: str

    @classmethod
    def of(cls, t: IncidentTransition) -> TransitionOut:
        return cls(
            from_status=None if t.from_status is None else t.from_status.value,
            to_status=t.to_status.value,
            at_utc=t.at_wall_utc.isoformat(),
            actor=t.actor,
            reason=t.reason,
        )


class EvidenceOut(_Out):
    evidence_id: str
    kind: str
    media_type: str
    bytes: int
    sha256: str
    captured_utc: str
    span_ms: list[float] | None
    expires_utc: str | None
    # Relative, never absolute: the data directory is movable (01 §10). The file is fetched
    # through the API by `evidence_id`, never by this path.
    relative_path: str

    @classmethod
    def of(cls, e: Evidence) -> EvidenceOut:
        return cls(
            evidence_id=e.evidence_id,
            kind=e.kind.value,
            media_type=e.media_type,
            bytes=e.size_bytes,
            sha256=e.sha256,
            captured_utc=e.captured_wall_utc.isoformat(),
            span_ms=None if e.span_ms is None else [e.span_ms[0], e.span_ms[1]],
            expires_utc=None if e.expires_wall_utc is None else e.expires_wall_utc.isoformat(),
            relative_path=e.relative_path,
        )


class ObservationOut(_Out):
    t_monotonic_ns: int
    signal: float
    facts: dict[str, float | str | bool]


class AccumulatorSampleOut(_Out):
    t_monotonic_ns: int
    log_odds: float
    confidence: float


class VerdictOut(_Out):
    validator_id: str
    outcome: str
    reason: str


class SeverityFactorOut(_Out):
    name: str
    raw_value: float
    weight: float
    contribution: float


class ExplanationOut(_Out):
    """The whole decision, reconstructable months later (01 §7.5)."""

    trigger_summary: str
    observations: list[ObservationOut]
    accumulator_trace: list[AccumulatorSampleOut]
    validator_verdicts: list[VerdictOut]
    severity_factors: list[SeverityFactorOut]
    thresholds: dict[str, float]
    capability_caveats: list[str]

    @classmethod
    def of(cls, incident: Incident) -> ExplanationOut:
        e = incident.explanation
        return cls(
            trigger_summary=e.trigger_summary,
            observations=[
                ObservationOut(
                    t_monotonic_ns=o.t_monotonic_ns, signal=o.signal, facts=dict(o.facts)
                )
                for o in e.observations
            ],
            accumulator_trace=[
                AccumulatorSampleOut(
                    t_monotonic_ns=s.t_monotonic_ns,
                    log_odds=s.log_odds,
                    confidence=s.confidence_ratio,
                )
                for s in e.accumulator_trace
            ],
            validator_verdicts=[
                VerdictOut(validator_id=v.validator_id, outcome=v.outcome.value, reason=v.reason)
                for v in e.validator_verdicts
            ],
            severity_factors=[
                SeverityFactorOut(
                    name=f.name,
                    raw_value=f.raw_value,
                    weight=f.weight,
                    contribution=f.contribution,
                )
                for f in e.severity_factors
            ],
            thresholds=dict(e.thresholds),
            capability_caveats=list(e.capability_caveats),
        )


class IncidentSummaryOut(_Out):
    incident_id: str
    display_ref: str
    camera_id: str
    event_type: str
    module_id: str
    module_version: str
    status: str
    severity: str
    severity_score: float
    confidence: float
    zone_ids: list[str]
    location: str | None
    first_observed_utc: str
    confirmed_utc: str
    last_observed_utc: str
    resolved_utc: str | None
    duration_ms: float | None
    detection_latency_ms: float
    evidence_count: int
    evidence_status: str | None
    has_note: bool

    @classmethod
    def of(
        cls, incident: Incident, *, evidence_count: int, evidence_status: str | None
    ) -> IncidentSummaryOut:
        return cls(
            incident_id=incident.incident_id,
            display_ref=incident.display_ref,
            camera_id=incident.camera_id,
            event_type=incident.event_type.value,
            module_id=incident.module_id,
            module_version=incident.module_version,
            status=incident.status.value,
            # The band and the number both travel: the band drives the UI, the number sorts
            # within a band (06 §8).
            severity=incident.severity.name,
            severity_score=incident.severity_score,
            confidence=incident.confidence_ratio,
            zone_ids=sorted(incident.zone_ids),
            location=incident.location,
            first_observed_utc=incident.first_observed_wall_utc.isoformat(),
            confirmed_utc=incident.confirmed_wall_utc.isoformat(),
            last_observed_utc=incident.last_observed_wall_utc.isoformat(),
            resolved_utc=(
                None
                if incident.resolved_wall_utc is None
                else incident.resolved_wall_utc.isoformat()
            ),
            duration_ms=incident.duration_ms,
            detection_latency_ms=incident.detection_latency_ms,
            evidence_count=evidence_count,
            evidence_status=evidence_status,
            has_note=bool(incident.operator_note),
        )


class ActionPermissionOut(_Out):
    allowed: bool
    reason: str | None


class IncidentDetailOut(_Out):
    incident: IncidentSummaryOut
    explanation: ExplanationOut
    transitions: list[TransitionOut]
    evidence: list[EvidenceOut]
    operator_note: str | None
    involved_track_ids: list[int]
    tracker_epoch: int
    model: dict[str, str | int | None]
    system: dict[str, str | float | None]
    available_transitions: list[str]
    permissions: dict[str, ActionPermissionOut]


class IncidentPageOut(_Out):
    incidents: list[IncidentSummaryOut]
    total: int
    # Keyset rather than offset: the list is written to while an operator pages through it, and
    # OFFSET silently skips or repeats rows when that happens (08 §10).
    next_cursor: str | None
    page_size: int


class CandidateOut(_Out):
    """The rejection feed. During tuning this is the most informative view in the system."""

    candidate_id: str
    camera_id: str
    module_id: str
    event_type: str
    subject: str
    outcome: str
    rejection_reason: str | None
    confidence: float
    log_odds: float
    support_ratio: float
    observations: int
    promoted_utc: str
    incident_id: str | None

    @classmethod
    def of(cls, row: CandidateRow) -> CandidateOut:
        return cls(
            candidate_id=row.candidate_id,
            camera_id=row.camera_id,
            module_id=row.module_id,
            event_type=row.event_type.value,
            subject=row.subject,
            outcome=row.outcome.value,
            rejection_reason=row.rejection_reason,
            confidence=row.confidence_ratio,
            log_odds=row.log_odds,
            support_ratio=row.support_ratio,
            observations=row.observations,
            promoted_utc=row.promoted_wall_utc.isoformat(),
            incident_id=row.incident_id,
        )


class HealthEventOut(_Out):
    camera_id: str
    state: str
    at_utc: str
    detail: str | None

    @classmethod
    def of(cls, event: HealthEvent) -> HealthEventOut:
        return cls(
            camera_id=event.camera_id,
            state=event.state.value,
            at_utc=event.at_wall_utc.isoformat(),
            detail=event.detail,
        )


class TimelineOut(_Out):
    """Incidents and camera-health events on one axis: the most useful correlation here (08 §3)."""

    since_utc: str
    until_utc: str
    incidents: list[IncidentSummaryOut]
    health_events: list[HealthEventOut]


class MuteOut(_Out):
    mute_id: str
    camera_id: str | None
    zone_id: str | None
    event_type: str | None
    created_utc: str
    expires_utc: str
    remaining_s: float
    reason: str
    actor: str
    active: bool

    @classmethod
    def of(cls, record: MuteRecord, *, remaining_s: float) -> MuteOut:
        return cls(
            mute_id=record.mute_id,
            camera_id=record.camera_id,
            zone_id=record.zone_id,
            event_type=None if record.event_type is None else record.event_type.value,
            created_utc=record.created_wall_utc.isoformat(),
            expires_utc=record.expires_wall_utc.isoformat(),
            # The countdown the status strip renders. A mute is never silent (06 §10).
            remaining_s=remaining_s,
            reason=record.reason,
            actor=record.actor,
            active=remaining_s > 0.0 and record.revoked_wall_utc is None,
        )


class ModuleAvailabilityOut(_Out):
    """Why an event type is or is not offered on a camera (04 §4, 08 §8)."""

    module_id: str
    event_type: str
    version: str
    enabled: bool
    status: str
    will_run: bool
    requires: list[str]
    missing_required: list[str]
    missing_optional: list[str]
    reason: str | None
    description: str
    caveats: list[str]


class ChannelStatusOut(_Out):
    channel_id: str
    available: bool
    reason: str | None


def transitions_out(transitions: Sequence[IncidentTransition]) -> list[TransitionOut]:
    return [TransitionOut.of(t) for t in transitions]


def evidence_out(records: Sequence[Evidence]) -> list[EvidenceOut]:
    return [EvidenceOut.of(e) for e in records]


def model_out(incident: Incident) -> dict[str, str | int | None]:
    m = incident.model_metadata
    return {
        "name": m.name,
        "version": m.version,
        "backend": m.backend,
        "input_size_px": m.input_size_px,
        "precision": m.precision,
        "device": m.device,
        "license": m.license,
        "weights_sha256": m.weights_sha256,
    }


def system_out(incident: Incident) -> dict[str, str | float | None]:
    s = incident.system_metadata
    return {
        "version": s.version,
        # The sha256 of the effective config, which is what makes "why did this fire in March
        # but not now" answerable (05 §4).
        "config_hash": s.config_hash,
        "device": s.device,
        "pipeline_fps": s.pipeline_fps,
        "inference_latency_p95_ms": s.inference_latency_p95_ms,
    }


def permissions_out(
    permissions: Mapping[str, tuple[bool, str | None]],
) -> dict[str, ActionPermissionOut]:
    return {
        action: ActionPermissionOut(allowed=allowed, reason=reason)
        for action, (allowed, reason) in permissions.items()
    }
