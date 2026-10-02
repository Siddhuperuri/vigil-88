from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from vigil.domain import (
    EventType,
    Incident,
    IncidentExplanation,
    IncidentStatus,
    IncidentTransition,
    ModelDescriptor,
    Severity,
    SystemSnapshot,
)

T0 = datetime(2026, 10, 1, 2, 41, tzinfo=UTC)
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
MODEL = ModelDescriptor("null", "1", "null", None, None, "none", "none", None)


def incident(**kw: object) -> Incident:
    base: dict[str, object] = dict(
        incident_id=ULID, display_ref="INC-20261001-5FAV00", camera_id="cam-a",
        event_type=EventType.INTRUSION, module_id="intrusion", module_version="1.0.0",
        status=IncidentStatus.CONFIRMED, severity=Severity.HIGH, severity_score=61.9,
        confidence_ratio=0.92, zone_ids={"yard"}, location="North gate",
        first_observed_wall_utc=T0, confirmed_wall_utc=T0 + timedelta(milliseconds=900),
        last_observed_wall_utc=T0 + timedelta(seconds=1), resolved_wall_utc=None, duration_ms=None,
        involved_track_ids={42}, tracker_epoch=1, evidence_ids=(),
        explanation=IncidentExplanation("person entered restricted zone", (), (), (), (), {}, ()),
        transitions=(), model_metadata=MODEL,
        system_metadata=SystemSnapshot("0.1.0", "abc123", "none", None, None), operator_note=None,
    )
    return Incident(**{**base, **kw})  # type: ignore[arg-type]


def test_detection_latency_is_confirmed_minus_first_observed() -> None:
    assert incident().detection_latency_ms == pytest.approx(900.0)


def test_collections_are_frozen_on_construction() -> None:
    i = incident()
    assert isinstance(i.zone_ids, frozenset) and isinstance(i.involved_track_ids, frozenset)
    assert isinstance(i.evidence_ids, tuple) and isinstance(i.transitions, tuple)


@pytest.mark.parametrize(
    "bad",
    [
        {"incident_id": "nope"},
        {"display_ref": "INC-1"},
        {"display_ref": "inc-20261001-5FAV00"},
        {"severity_score": 101.0},
        {"severity_score": -1.0},
        {"confidence_ratio": 1.2},
        {"confirmed_wall_utc": T0 - timedelta(seconds=1)},
        {"last_observed_wall_utc": T0 - timedelta(seconds=1)},
        {"resolved_wall_utc": T0},  # before confirmation
        {"duration_ms": -1.0},
        {"first_observed_wall_utc": datetime(2026, 10, 1)},  # noqa: DTZ001
    ],
)
def test_incident_invariants(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        incident(**bad)


def test_resolved_incident_is_valid() -> None:
    i = incident(
        status=IncidentStatus.RESOLVED,
        resolved_wall_utc=T0 + timedelta(seconds=20),
        duration_ms=19_700.0,
    )
    assert i.status is IncidentStatus.RESOLVED


def test_incident_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        incident().status = IncidentStatus.RESOLVED  # type: ignore[misc]


def test_transition_needs_a_reason_and_aware_time() -> None:
    IncidentTransition(None, IncidentStatus.CONFIRMED, T0, "system", "verified")
    with pytest.raises(ValueError):
        IncidentTransition(None, IncidentStatus.CONFIRMED, T0, "system", "")
    with pytest.raises(ValueError):
        IncidentTransition(None, IncidentStatus.CONFIRMED, datetime(2026, 1, 1), "system", "x")  # noqa: DTZ001
