"""A report as a JSON document (10-roadmap §8).

Split from `export.py` because the two answer different questions. This file answers "what does
a report look like as data", once, and both the file export and the API's `/analytics/report`
route use the same answer -- so a downloaded file and an HTTP response can never disagree about
the shape of a report. `export.py` answers "how is it written out safely".

The schema is versioned (`vigil-88.report.v1`) because the document is meant to leave the
system: something else will parse it, and a consumer that cannot tell which shape it is holding
has to guess.

**Evidence is referenced, never embedded.** A report carries evidence ids and relative paths;
the bytes are fetched through the API, which is the only path that re-resolves the stored path
and verifies the hash (05 §6).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TextIO

from vigil.analytics.definitions import Count, DurationStat, MetricDefinition
from vigil.analytics.queries import Window
from vigil.analytics.reports import IncidentRow, Report, Summary
from vigil.persistence.queries import IncidentFilter


def _window_json(window: Window) -> dict[str, object]:
    return {
        "since_utc": window.since.isoformat(),
        "until_utc": window.until.isoformat(),
        "utc_offset_minutes": window.utc_offset_minutes,
        "duration_s": window.duration_s,
    }


def filter_json(filters: IncidentFilter) -> dict[str, object]:
    """The filter, echoed back, so a reader can tell what the numbers are *about*."""
    return {
        "camera_ids": list(filters.camera_ids),
        "event_types": [t.value for t in filters.event_types],
        "statuses": [s.value for s in filters.statuses],
        "min_severity": None if filters.min_severity is None else filters.min_severity.name,
        "zone_id": filters.zone_id,
        "search": filters.search,
        "open_only": filters.open_only,
        "has_evidence": filters.has_evidence,
    }


def _counts_json(counts: Sequence[Count]) -> list[dict[str, object]]:
    return [{"key": c.key, "count": c.count} for c in counts]


def _duration_json(stat: DurationStat) -> dict[str, object]:
    return {
        "available": stat.available,
        "count": stat.count,
        "mean_ms": stat.mean_ms,
        "p50_ms": stat.p50_ms,
        "p95_ms": stat.p95_ms,
        "max_ms": stat.max_ms,
    }


def _definitions_json(
    definitions: Mapping[str, MetricDefinition],
) -> dict[str, dict[str, str]]:
    return {
        name: {
            "population": d.population,
            "definition": d.definition,
            "unit": d.unit,
        }
        for name, d in sorted(definitions.items())
    }


def _summary_json(summary: Summary) -> dict[str, object]:
    return {
        "incidents_total": summary.incidents_total,
        "open_now": summary.open_now,
        "by_event_type": _counts_json(summary.by_event_type),
        "by_severity": _counts_json(summary.by_severity),
        "by_status": _counts_json(summary.by_status),
        "by_camera": _counts_json(summary.by_camera),
        "trend": {
            "bucket": summary.trend_bucket.value,
            "points": _counts_json(summary.trend),
        },
        "duration": _duration_json(summary.duration),
        "detection_latency": _duration_json(summary.detection_latency),
        "acknowledgement": _duration_json(summary.acknowledgement),
        "resolution": _duration_json(summary.resolution),
        "dismissal_rate": [
            {
                "module_id": module_id,
                "module_version": version,
                "dismissed": dismissed,
                "total": total,
                "ratio": ratio,
            }
            for module_id, version, dismissed, total, ratio in summary.dismissal_rate
        ],
        "candidate_outcomes": _counts_json(summary.candidate_outcomes),
        "rejection_reasons": _counts_json(summary.rejection_reasons),
        "camera_availability": {
            camera: (None if totals is None else dict(totals))
            for camera, totals in sorted(summary.camera_availability.items())
        },
    }


def _incident_json(row: IncidentRow) -> dict[str, object]:
    incident = row.incident
    explanation = incident.explanation
    return {
        "display_ref": incident.display_ref,
        "incident_id": incident.incident_id,
        "camera_id": incident.camera_id,
        "location": incident.location,
        "event_type": incident.event_type.value,
        "module_id": incident.module_id,
        "module_version": incident.module_version,
        "status": incident.status.value,
        "severity": incident.severity.name,
        "severity_score": incident.severity_score,
        "confidence_ratio": incident.confidence_ratio,
        "first_observed_utc": incident.first_observed_wall_utc.isoformat(),
        "confirmed_utc": incident.confirmed_wall_utc.isoformat(),
        "last_observed_utc": incident.last_observed_wall_utc.isoformat(),
        "resolved_utc": (
            None if incident.resolved_wall_utc is None else incident.resolved_wall_utc.isoformat()
        ),
        "detection_latency_ms": incident.detection_latency_ms,
        "duration_ms": incident.duration_ms,
        "zone_ids": sorted(incident.zone_ids),
        "involved_track_ids": sorted(incident.involved_track_ids),
        "tracker_epoch": incident.tracker_epoch,
        "operator_note": incident.operator_note,
        # The timeline: every transition with who made it and why.
        "timeline": [
            {
                "from_status": None if t.from_status is None else t.from_status.value,
                "to_status": t.to_status.value,
                "at_utc": t.at_wall_utc.isoformat(),
                "actor": t.actor,
                "reason": t.reason,
            }
            for t in incident.transitions
        ],
        "explanation": {
            "trigger_summary": explanation.trigger_summary,
            "thresholds": dict(explanation.thresholds),
            "capability_caveats": list(explanation.capability_caveats),
            "severity_factors": [
                {
                    "name": f.name,
                    "raw_value": f.raw_value,
                    "weight": f.weight,
                    "contribution": f.contribution,
                }
                for f in explanation.severity_factors
            ],
            "validator_verdicts": [
                {"validator_id": v.validator_id, "outcome": v.outcome.value, "reason": v.reason}
                for v in explanation.validator_verdicts
            ],
            "observations": [
                {
                    "t_monotonic_ns": o.t_monotonic_ns,
                    "signal": o.signal,
                    "facts": dict(o.facts),
                }
                for o in explanation.observations
            ],
        },
        # Referenced, never embedded: files are fetched through the API, which re-checks the
        # path and verifies the hash (05 §6).
        "evidence": {
            "status": row.evidence_status,
            "count": len(row.evidence_ids),
            "evidence_ids": list(row.evidence_ids),
            "relative_paths": list(row.evidence_paths),
        },
        "model": {
            # No secret ever reaches an export: a `ModelDescriptor` carries a name, a backend and
            # a hash, and `SourceSpec.locator` -- the only credential-adjacent field in the
            # system -- is never copied onto an incident (05 §4).
            "name": incident.model_metadata.name,
            "version": incident.model_metadata.version,
            "backend": incident.model_metadata.backend,
            "device": incident.model_metadata.device,
            "precision": incident.model_metadata.precision,
            "input_size_px": incident.model_metadata.input_size_px,
            "license": incident.model_metadata.license,
            "weights_sha256": incident.model_metadata.weights_sha256,
        },
        "system": {
            "version": incident.system_metadata.version,
            "config_hash": incident.system_metadata.config_hash,
            "device": incident.system_metadata.device,
            "pipeline_fps": incident.system_metadata.pipeline_fps,
            "inference_latency_p95_ms": incident.system_metadata.inference_latency_p95_ms,
        },
    }


def report_to_json(report: Report) -> dict[str, object]:
    return {
        "schema": "vigil-88.report.v1",
        "generated_wall_utc": report.generated_wall_utc.isoformat(),
        "window": _window_json(report.window),
        "filters": filter_json(report.filters),
        "coverage": {
            "covers_full_window": report.covers_full_window,
            "earliest_record_utc": (
                None if report.earliest_record is None else report.earliest_record.isoformat()
            ),
            "truncated": report.truncated,
            "row_limit": report.row_limit,
            "rows_included": len(report.incidents),
        },
        "caveats": list(report.caveats),
        "summary": _summary_json(report.summary),
        "metric_definitions": _definitions_json(report.definitions),
        "incidents": [_incident_json(row) for row in report.incidents],
    }


def write_json(report: Report, handle: TextIO) -> int:
    """Write the report as one JSON document. Returns the number of incident rows."""
    json.dump(report_to_json(report), handle, indent=2, sort_keys=False, default=str)
    handle.write("\n")
    return len(report.incidents)
