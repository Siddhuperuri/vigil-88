"""The only translator between domain objects and database rows (02 §4).

`domain/` is frozen, pure and knows nothing about storage; rows are flat and shaped by schema
concerns. Fusing them — the active-record shortcut — makes every schema migration an engine
refactor. This file is the price of keeping them apart, and it is deliberately boring: explicit
field lists, no reflection, no inheritance.

Timestamps are stored as ISO-8601 with an explicit offset, which sorts lexicographically in the
same order as chronologically for a fixed offset, and we always write UTC. Nested structures
(the explanation, verdicts, the accumulator trace) are JSON columns: they are read whole, with
one incident, and never queried into — so a column per field would buy nothing and cost a
migration every time an explanation gains a term.

Reading is defensive. A row written by an older build may be missing a value a newer domain type
requires; rather than crash the incident list, `incident_from_row` raises `PersistenceError`
naming the incident and the field, and the caller decides (the API skips it and says so).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from vigil.core.errors import PersistenceError
from vigil.domain.detection import ModelDescriptor
from vigil.domain.enums import (
    CandidateOutcome,
    EventType,
    EvidenceKind,
    IncidentStatus,
    Severity,
    ValidatorOutcome,
)
from vigil.domain.event import AccumulatorSample, CandidateRecord, ValidatorVerdict
from vigil.domain.evidence import Evidence
from vigil.domain.incident import (
    Actor,
    Incident,
    IncidentExplanation,
    IncidentTransition,
    ObservationSummary,
    SeverityFactor,
    SystemSnapshot,
)
from vigil.domain.observation import FactValue, SubjectKey, SubjectKind

JsonValue = Any


class Row(Protocol):
    """A database row, read by column name.

    A Protocol rather than `sqlite3.Row` so a test can hand these functions a plain dict and
    exercise the mapping without a database (09 §12).
    """

    def __getitem__(self, key: str) -> Any: ...


def to_utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def from_utc_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_utc(value: str | None) -> datetime | None:
    return None if value is None else from_utc_text(value)


SUBJECT_KINDS: frozenset[str] = frozenset({"track", "zone", "region", "camera"})


def dumps(value: JsonValue) -> str:
    """Compact, key-sorted JSON so a stored blob is byte-stable for a given value."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def loads(text: str, *, field: str, owner: str) -> JsonValue:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(
            f"stored {field} for {owner} is not valid JSON", context={"error": str(exc)}
        ) from exc


# ---------------------------------------------------------------- explanation


def _fact_value(value: JsonValue) -> FactValue:
    if isinstance(value, bool | float | str):
        return value
    if isinstance(value, int):
        return float(value)
    return str(value)


def explanation_to_json(explanation: IncidentExplanation) -> JsonValue:
    return {
        "trigger_summary": explanation.trigger_summary,
        "observations": [
            {"t_monotonic_ns": o.t_monotonic_ns, "signal": o.signal, "facts": dict(o.facts)}
            for o in explanation.observations
        ],
        "accumulator_trace": [
            {
                "t_monotonic_ns": s.t_monotonic_ns,
                "log_odds": s.log_odds,
                "confidence_ratio": s.confidence_ratio,
            }
            for s in explanation.accumulator_trace
        ],
        "validator_verdicts": [
            {"validator_id": v.validator_id, "outcome": v.outcome.value, "reason": v.reason}
            for v in explanation.validator_verdicts
        ],
        "severity_factors": [
            {
                "name": f.name,
                "raw_value": f.raw_value,
                "weight": f.weight,
                "contribution": f.contribution,
            }
            for f in explanation.severity_factors
        ],
        "thresholds": dict(explanation.thresholds),
        "capability_caveats": list(explanation.capability_caveats),
    }


def explanation_from_json(data: JsonValue) -> IncidentExplanation:
    return IncidentExplanation(
        trigger_summary=str(data["trigger_summary"]),
        observations=tuple(
            ObservationSummary(
                t_monotonic_ns=int(o["t_monotonic_ns"]),
                signal=float(o["signal"]),
                facts={k: _fact_value(v) for k, v in dict(o.get("facts", {})).items()},
            )
            for o in data.get("observations", ())
        ),
        accumulator_trace=tuple(
            AccumulatorSample(
                t_monotonic_ns=int(s["t_monotonic_ns"]),
                log_odds=float(s["log_odds"]),
                confidence_ratio=float(s["confidence_ratio"]),
            )
            for s in data.get("accumulator_trace", ())
        ),
        validator_verdicts=tuple(verdicts_from_json(data.get("validator_verdicts", ()))),
        severity_factors=tuple(
            SeverityFactor(
                name=str(f["name"]),
                raw_value=float(f["raw_value"]),
                weight=float(f["weight"]),
                contribution=float(f["contribution"]),
            )
            for f in data.get("severity_factors", ())
        ),
        thresholds={str(k): float(v) for k, v in dict(data.get("thresholds", {})).items()},
        capability_caveats=tuple(str(c) for c in data.get("capability_caveats", ())),
    )


def verdicts_to_json(verdicts: Sequence[ValidatorVerdict]) -> JsonValue:
    return [
        {"validator_id": v.validator_id, "outcome": v.outcome.value, "reason": v.reason}
        for v in verdicts
    ]


def verdicts_from_json(data: JsonValue) -> tuple[ValidatorVerdict, ...]:
    return tuple(
        ValidatorVerdict(
            validator_id=str(v["validator_id"]),
            outcome=ValidatorOutcome(v["outcome"]),
            reason=str(v["reason"]),
        )
        for v in data
    )


def model_to_json(model: ModelDescriptor) -> JsonValue:
    return {
        "name": model.name,
        "version": model.version,
        "backend": model.backend,
        "weights_sha256": model.weights_sha256,
        "input_size_px": model.input_size_px,
        "precision": model.precision,
        "device": model.device,
        "license": model.license,
    }


def model_from_json(data: JsonValue) -> ModelDescriptor:
    size = data.get("input_size_px")
    return ModelDescriptor(
        name=str(data["name"]),
        version=str(data.get("version", "")),
        backend=str(data["backend"]),
        weights_sha256=data.get("weights_sha256"),
        input_size_px=None if size is None else int(size),
        precision=str(data.get("precision", "")),
        device=str(data.get("device", "")),
        license=data.get("license"),
    )


def system_to_json(snapshot: SystemSnapshot) -> JsonValue:
    return {
        "version": snapshot.version,
        "config_hash": snapshot.config_hash,
        "device": snapshot.device,
        "pipeline_fps": snapshot.pipeline_fps,
        "inference_latency_p95_ms": snapshot.inference_latency_p95_ms,
    }


def system_from_json(data: JsonValue) -> SystemSnapshot:
    fps = data.get("pipeline_fps")
    p95 = data.get("inference_latency_p95_ms")
    return SystemSnapshot(
        version=str(data["version"]),
        config_hash=str(data["config_hash"]),
        device=str(data.get("device", "")),
        pipeline_fps=None if fps is None else float(fps),
        inference_latency_p95_ms=None if p95 is None else float(p95),
    )


# ---------------------------------------------------------------- incident


def incident_to_row(incident: Incident, *, dedup_key: str, candidate_id: str) -> dict[str, object]:
    return {
        "incident_id": incident.incident_id,
        "display_ref": incident.display_ref,
        "camera_id": incident.camera_id,
        "event_type": incident.event_type.value,
        "module_id": incident.module_id,
        "module_version": incident.module_version,
        "status": incident.status.value,
        "severity": int(incident.severity),
        "severity_score": incident.severity_score,
        "confidence_ratio": incident.confidence_ratio,
        "first_observed_utc": to_utc_text(incident.first_observed_wall_utc),
        "confirmed_utc": to_utc_text(incident.confirmed_wall_utc),
        "last_observed_utc": to_utc_text(incident.last_observed_wall_utc),
        "resolved_utc": (
            None if incident.resolved_wall_utc is None else to_utc_text(incident.resolved_wall_utc)
        ),
        "duration_ms": incident.duration_ms,
        "zone_ids_json": dumps(sorted(incident.zone_ids)),
        "location": incident.location,
        "involved_track_ids_json": dumps(sorted(incident.involved_track_ids)),
        "tracker_epoch": incident.tracker_epoch,
        "explanation_json": dumps(explanation_to_json(incident.explanation)),
        "model_metadata_json": dumps(model_to_json(incident.model_metadata)),
        "system_metadata_json": dumps(system_to_json(incident.system_metadata)),
        "operator_note": incident.operator_note,
        "dedup_key": dedup_key,
        "candidate_id": candidate_id,
    }


def incident_from_row(row: Row, transitions: Sequence[IncidentTransition] = ()) -> Incident:
    incident_id = str(row["incident_id"])
    try:
        return Incident(
            incident_id=incident_id,
            display_ref=str(row["display_ref"]),
            camera_id=str(row["camera_id"]),
            event_type=EventType(row["event_type"]),
            module_id=str(row["module_id"]),
            module_version=str(row["module_version"]),
            status=IncidentStatus(row["status"]),
            severity=Severity(int(row["severity"])),
            severity_score=float(row["severity_score"]),
            confidence_ratio=float(row["confidence_ratio"]),
            zone_ids=frozenset(loads(row["zone_ids_json"], field="zone_ids", owner=incident_id)),
            location=row["location"],
            first_observed_wall_utc=from_utc_text(row["first_observed_utc"]),
            confirmed_wall_utc=from_utc_text(row["confirmed_utc"]),
            last_observed_wall_utc=from_utc_text(row["last_observed_utc"]),
            resolved_wall_utc=_optional_utc(row["resolved_utc"]),
            duration_ms=None if row["duration_ms"] is None else float(row["duration_ms"]),
            involved_track_ids=frozenset(
                int(t)
                for t in loads(
                    row["involved_track_ids_json"], field="involved_track_ids", owner=incident_id
                )
            ),
            tracker_epoch=int(row["tracker_epoch"]),
            evidence_ids=(),
            explanation=explanation_from_json(
                loads(row["explanation_json"], field="explanation", owner=incident_id)
            ),
            transitions=tuple(transitions),
            model_metadata=model_from_json(
                loads(row["model_metadata_json"], field="model_metadata", owner=incident_id)
            ),
            system_metadata=system_from_json(
                loads(row["system_metadata_json"], field="system_metadata", owner=incident_id)
            ),
            operator_note=row["operator_note"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(
            f"stored incident {incident_id} cannot be read by this build",
            context={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc


def transition_to_row(incident_id: str, transition: IncidentTransition) -> dict[str, object]:
    return {
        "incident_id": incident_id,
        "from_status": None if transition.from_status is None else transition.from_status.value,
        "to_status": transition.to_status.value,
        "at_utc": to_utc_text(transition.at_wall_utc),
        "actor": transition.actor,
        "reason": transition.reason,
    }


def transition_from_row(row: Row) -> IncidentTransition:
    actor: Actor = "operator" if row["actor"] == "operator" else "system"
    return IncidentTransition(
        from_status=None if row["from_status"] is None else IncidentStatus(row["from_status"]),
        to_status=IncidentStatus(row["to_status"]),
        at_wall_utc=from_utc_text(row["at_utc"]),
        actor=actor,
        reason=str(row["reason"]),
    )


# ---------------------------------------------------------------- evidence


def evidence_to_row(evidence: Evidence) -> dict[str, object]:
    span = evidence.span_ms
    return {
        "evidence_id": evidence.evidence_id,
        "incident_id": evidence.incident_id,
        "kind": evidence.kind.value,
        "relative_path": evidence.relative_path,
        "sha256": evidence.sha256,
        "bytes": evidence.size_bytes,
        "media_type": evidence.media_type,
        "captured_utc": to_utc_text(evidence.captured_wall_utc),
        "span_start_ms": None if span is None else span[0],
        "span_end_ms": None if span is None else span[1],
        "expires_utc": (
            None if evidence.expires_wall_utc is None else to_utc_text(evidence.expires_wall_utc)
        ),
    }


def evidence_from_row(row: Row) -> Evidence:
    start, end = row["span_start_ms"], row["span_end_ms"]
    return Evidence(
        evidence_id=str(row["evidence_id"]),
        incident_id=str(row["incident_id"]),
        kind=EvidenceKind(row["kind"]),
        relative_path=str(row["relative_path"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["bytes"]),
        media_type=str(row["media_type"]),
        captured_wall_utc=from_utc_text(row["captured_utc"]),
        span_ms=None if start is None or end is None else (float(start), float(end)),
        expires_wall_utc=_optional_utc(row["expires_utc"]),
    )


# ---------------------------------------------------------------- candidates


def candidate_to_row(record: CandidateRecord, *, incident_id: str | None) -> dict[str, object]:
    candidate = record.candidate
    accumulator = candidate.accumulator
    return {
        "candidate_id": candidate.candidate_id,
        "camera_id": candidate.camera_id,
        "module_id": candidate.module_id,
        "event_type": candidate.event_type.value,
        "subject": str(candidate.subject),
        "log_odds": accumulator.log_odds,
        "confidence_ratio": accumulator.confidence_ratio,
        "support_ratio": accumulator.support_ratio,
        "observations": accumulator.observation_count,
        "promoted_utc": to_utc_text(candidate.promoted_wall_utc),
        "outcome": record.outcome.value,
        "rejection_reason": record.rejection_reason,
        "verdicts_json": dumps(verdicts_to_json(record.verdicts)),
        "trace_json": dumps(
            [
                {
                    "t_monotonic_ns": s.t_monotonic_ns,
                    "log_odds": s.log_odds,
                    "confidence_ratio": s.confidence_ratio,
                }
                for s in record.accumulator_trace
            ]
        ),
        "incident_id": incident_id,
    }


def subject_from_text(text: str) -> SubjectKey:
    kind, _, value = text.partition(":")
    if kind not in SUBJECT_KINDS:
        raise PersistenceError(f"stored subject {text!r} has an unknown kind")
    return SubjectKey(cast(SubjectKind, kind), value)


def candidate_outcome(text: str) -> CandidateOutcome:
    return CandidateOutcome(text)
