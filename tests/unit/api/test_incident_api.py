"""The incident API: filtering, pagination, actions, evidence serving and honest absence.

These drive the real FastAPI app over a real database, because the thing most likely to be
wrong is the translation at the boundary -- a filter that means something different in the list
than in the count, a cursor that skips a row, an action offered that the lifecycle forbids.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from vigil.api.app import create_app
from vigil.api.routers.incidents import INTEGRITY_HEADER
from vigil.api.server import ApiServer
from vigil.config.loader import LoadedConfig
from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import ManualClock
from vigil.core.ids import UlidFactory, display_ref
from vigil.core.rng import seeded_rng
from vigil.domain.detection import ModelDescriptor
from vigil.domain.enums import EventType, EvidenceKind, IncidentStatus, Severity
from vigil.domain.evidence import Evidence
from vigil.domain.incident import (
    Incident,
    IncidentExplanation,
    IncidentTransition,
    ObservationSummary,
    SeverityFactor,
    SystemSnapshot,
)
from vigil.domain.observation import SubjectKey
from vigil.observability.logging import get_logger
from vigil.pipeline.app import Application
from vigil.pipeline.response_stack import ResponseStack
from vigil.pipeline.streaming import StreamHub
from vigil.response.evidence import sha256_bytes
from vigil.response.incident_manager import dedup_key_text

MakeConfig = Callable[..., LoadedConfig]
MODEL = ModelDescriptor(
    "yolox_s", "1.0", "onnxruntime", "ab" * 32, 640, "fp32", "cuda", "Apache-2.0"
)
SYSTEM = SystemSnapshot("0.1.0", "c" * 64, "cuda", 14.2, 62.0)
CAMERA = "gate-north"
WALL = datetime(2026, 3, 1, 2, 41, tzinfo=UTC)


def _explanation() -> IncidentExplanation:
    return IncidentExplanation(
        trigger_summary="intrusion reported for a person (track:42) in zone yard",
        observations=(ObservationSummary(1_000, 0.7, {"zone_id": "yard", "hits": 5.0}),),
        accumulator_trace=(),
        validator_verdicts=(),
        severity_factors=(SeverityFactor("base", 0.65, 0.3, 0.195),),
        thresholds={"temporal.l_activate": 2.2},
        capability_caveats=("intrusion: running without velocity",),
    )


def _incident(
    ulids: UlidFactory,
    *,
    camera_id: str = CAMERA,
    event_type: EventType = EventType.INTRUSION,
    severity: Severity = Severity.HIGH,
    score: float = 61.9,
    status: IncidentStatus = IncidentStatus.CONFIRMED,
    confirmed: datetime = WALL,
    note: str | None = None,
) -> Incident:
    incident_id = ulids.new()
    return Incident(
        incident_id=incident_id,
        display_ref=display_ref(incident_id, confirmed),
        camera_id=camera_id,
        event_type=event_type,
        module_id="intrusion",
        module_version="1.0.0",
        status=status,
        severity=severity,
        severity_score=score,
        confidence_ratio=0.92,
        zone_ids=frozenset({"yard"}),
        location="North gate, pole 3",
        first_observed_wall_utc=confirmed - timedelta(milliseconds=900),
        confirmed_wall_utc=confirmed,
        last_observed_wall_utc=confirmed + timedelta(seconds=5),
        resolved_wall_utc=None,
        duration_ms=None,
        involved_track_ids=frozenset({42}),
        tracker_epoch=1,
        evidence_ids=(),
        explanation=_explanation(),
        transitions=(
            IncidentTransition(None, IncidentStatus.CANDIDATE, confirmed, "system", "promoted"),
            IncidentTransition(
                IncidentStatus.CANDIDATE, status, confirmed, "system", "scored and persisted"
            ),
        ),
        model_metadata=MODEL,
        system_metadata=SYSTEM,
        operator_note=note,
    )


@pytest.fixture
def api(make_config: MakeConfig) -> Iterator[tuple[httpx.Client, ResponseStack, Path]]:
    """The real app and the real API over a real database, with no camera configured.

    The routes are what is under test, so the pipeline has nothing to do; the incident store is
    the application's own, seeded directly through its repositories.
    """
    loaded = make_config({"cameras": []})
    application = Application(loaded.settings, loaded.paths, clock=ManualClock())
    application.start()
    response = application.response
    assert response is not None
    hub = StreamHub()
    server = ApiServer(
        create_app(application, hub), host="127.0.0.1", port=0, logger=get_logger("test.api")
    )
    server.start()
    try:
        with httpx.Client(base_url=f"{server.url}/api/v1", timeout=15) as client:
            yield (client, response, loaded.paths.evidence_dir)
    finally:
        server.stop()
        hub.close()
        application.stop()


def _ulids() -> UlidFactory:
    return UlidFactory(ManualClock(), seeded_rng(11, "seed"))


def _seed(response: ResponseStack, ulids: UlidFactory, incident: Incident) -> Incident:
    response.incidents.insert(
        incident,
        dedup_key=dedup_key_text(
            incident.camera_id, incident.event_type, SubjectKey("track", "42")
        ),
        candidate_id=ulids.new(),
        evidence_status="complete",
        now_wall_utc=incident.confirmed_wall_utc,
    )
    return incident


@pytest.fixture
def seeded(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> tuple[httpx.Client, ResponseStack, UlidFactory, Incident]:
    client, response, _root = api
    ulids = _ulids()
    return (client, response, ulids, _seed(response, ulids, _incident(ulids)))


# ---------------------------------------------------------------- listing


def test_an_empty_store_is_an_empty_page_not_an_error(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _root = api
    body = client.get("/incidents").json()
    assert body["incidents"] == []
    assert body["total"] == 0
    assert body["next_cursor"] is None


def test_health_says_whether_incidents_are_being_persisted(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _root = api
    body = client.get("/system/health").json()
    assert body["persists_incidents"] is True
    assert body["schema_version"] >= 1


def test_a_listed_incident_carries_what_a_row_needs(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, incident = seeded
    body = client.get("/incidents").json()
    assert body["total"] == 1
    row = body["incidents"][0]
    assert row["display_ref"] == incident.display_ref
    assert row["severity"] == "HIGH"  # the band, for the UI
    assert row["severity_score"] == pytest.approx(61.9)  # and the number, for sorting
    assert row["event_type"] == "intrusion"
    assert row["location"] == "North gate, pole 3"
    assert row["detection_latency_ms"] == pytest.approx(900.0, abs=1.0)


def test_filters_narrow_the_list_and_the_total_together(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, response, ulids, _first = seeded
    _seed(response, ulids, _incident_other(ulids))

    both = client.get("/incidents").json()
    assert both["total"] == 2

    narrowed = client.get("/incidents", params={"event_type": "person_down"}).json()
    assert narrowed["total"] == 1, "the total reflects the filter, not the table"
    assert narrowed["incidents"][0]["event_type"] == "person_down"

    by_camera = client.get("/incidents", params={"camera_id": "dock-west"}).json()
    assert by_camera["total"] == 1

    severe = client.get("/incidents", params={"min_severity": "CRITICAL"}).json()
    assert severe["total"] == 0


def _incident_other(ulids: UlidFactory) -> Incident:
    return _incident(
        ulids,
        camera_id="dock-west",
        event_type=EventType.PERSON_DOWN,
        severity=Severity.MODERATE,
        score=44.0,
        confirmed=WALL + timedelta(minutes=5),
    )


def test_an_unknown_filter_value_is_a_422_naming_it(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _root = api
    body = client.get("/incidents", params={"event_type": "nonsense"})
    assert body.status_code == 422
    assert "nonsense" in body.json()["detail"]

    assert client.get("/incidents", params={"status": "sideways"}).status_code == 422
    assert client.get("/incidents", params={"min_severity": "URGENT"}).status_code == 422
    assert client.get("/incidents", params={"sort": "whatever"}).status_code == 422


def test_pagination_is_keyset_and_visits_every_row_once(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, _root = api
    ulids = UlidFactory(ManualClock(), seeded_rng(3, "page"))
    for minute in range(7):
        _seed(response, ulids, _incident(ulids, confirmed=WALL + timedelta(minutes=minute)))

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/incidents", params=params).json()
        seen.extend(r["incident_id"] for r in body["incidents"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 7
    assert len(set(seen)) == 7, "no row is repeated across pages"


def test_a_malformed_cursor_is_rejected(api: tuple[httpx.Client, ResponseStack, Path]) -> None:
    client, _response, _root = api
    assert client.get("/incidents", params={"cursor": "garbage"}).status_code == 422


def test_the_page_size_is_capped(api: tuple[httpx.Client, ResponseStack, Path]) -> None:
    client, _response, _root = api
    body = client.get("/incidents", params={"limit": 100_000}).json()
    assert body["page_size"] == StorageConfig().max_page_size


# ---------------------------------------------------------------- detail


def test_the_detail_reconstructs_the_decision(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, incident = seeded
    body = client.get(f"/incidents/{incident.incident_id}").json()

    assert body["explanation"]["trigger_summary"]
    assert body["explanation"]["thresholds"]["temporal.l_activate"] == 2.2
    assert body["explanation"]["capability_caveats"] == ["intrusion: running without velocity"]
    assert [t["to_status"] for t in body["transitions"]] == ["candidate", "confirmed"]
    assert body["model"]["name"] == "yolox_s"
    assert body["model"]["license"] == "Apache-2.0"
    assert len(body["system"]["config_hash"]) == 64
    assert body["involved_track_ids"] == [42]


def test_an_unknown_incident_is_a_404(api: tuple[httpx.Client, ResponseStack, Path]) -> None:
    client, _response, _root = api
    assert client.get("/incidents/01ARZ3NDEKTSV4RRFFQ69G5FAV").status_code == 404


def test_permissions_say_what_can_be_done_now(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    """The anti-decorative-control mechanism: the console disables rather than erroring."""
    client, _response, _ulids, incident = seeded
    body = client.get(f"/incidents/{incident.incident_id}").json()
    permissions = body["permissions"]
    assert permissions["acknowledge"]["allowed"] is True
    assert permissions["dismiss"]["allowed"] is True
    assert permissions["annotate"]["allowed"] is True
    assert "active" in body["available_transitions"]


# ---------------------------------------------------------------- actions


def test_acknowledge_moves_confirmed_to_active_and_records_who(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, incident = seeded
    body = client.post(
        f"/incidents/{incident.incident_id}/acknowledge",
        json={"reason": "on my way"},
    ).json()
    assert body["incident"]["status"] == "active"
    last = body["transitions"][-1]
    assert last["actor"] == "operator"
    assert last["reason"] == "on my way"


def test_acknowledging_twice_is_a_409_with_the_reason(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, incident = seeded
    client.post(f"/incidents/{incident.incident_id}/acknowledge")
    second = client.post(f"/incidents/{incident.incident_id}/acknowledge")
    assert second.status_code == 409
    assert "active" in second.json()["detail"]


def test_dismiss_is_terminal_and_closes_the_incident(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, incident = seeded
    body = client.post(
        f"/incidents/{incident.incident_id}/dismiss",
        json={"reason": "contractors, known"},
    ).json()
    assert body["incident"]["status"] == "dismissed"
    # Clamped, never earlier than `confirmed`: a backwards clock must not make a row
    # unreadable (see IncidentManager.transition).
    assert body["incident"]["resolved_utc"] >= body["incident"]["confirmed_utc"]
    assert body["incident"]["duration_ms"] >= 0.0
    assert body["permissions"]["acknowledge"]["allowed"] is False
    assert body["available_transitions"] == []


def test_escalate_raises_the_band_and_keeps_the_score(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    """The score is the severity engine's arithmetic; an override must not overwrite it."""
    client, _response, _ulids, incident = seeded
    body = client.post(f"/incidents/{incident.incident_id}/escalate").json()
    assert body["incident"]["severity"] == "CRITICAL"
    assert body["incident"]["severity_score"] == pytest.approx(61.9)
    again = client.post(f"/incidents/{incident.incident_id}/escalate")
    assert again.status_code == 409
    assert "CRITICAL" in again.json()["detail"]


def test_annotate_attaches_and_clears_a_note(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, incident = seeded
    body = client.post(
        f"/incidents/{incident.incident_id}/annotate",
        json={"note": "  spoke to the site manager  "},
    ).json()
    assert body["operator_note"] == "spoke to the site manager"  # stripped
    assert body["incident"]["has_note"] is True

    assert client.delete(f"/incidents/{incident.incident_id}/annotate").status_code == 204
    after = client.get(f"/incidents/{incident.incident_id}").json()
    assert after["operator_note"] is None


def test_an_over_long_note_is_refused(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, incident = seeded
    reply = client.post(f"/incidents/{incident.incident_id}/annotate", json={"note": "x" * 5000})
    assert reply.status_code == 400
    assert "2000" in reply.json()["detail"]


# ---------------------------------------------------------------- evidence


def test_evidence_is_served_with_an_integrity_verdict(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, ulids, incident = seeded
    _client, _response2, evidence_root = api

    payload = b'{"hello": "evidence"}'
    relative = f"{CAMERA}/2026/03/01/{incident.incident_id}"
    directory = evidence_root / relative
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "explanation.json").write_bytes(payload)
    record = Evidence(
        evidence_id=ulids.new(),
        incident_id=incident.incident_id,
        kind=EvidenceKind.EXPLANATION,
        relative_path=f"{relative}/explanation.json",
        sha256=sha256_bytes(payload),
        size_bytes=len(payload),
        media_type="application/json",
        captured_wall_utc=WALL,
        span_ms=None,
        expires_wall_utc=None,
    )
    response.evidence.add_all([record])

    listing = client.get(f"/incidents/{incident.incident_id}/evidence").json()
    assert [e["kind"] for e in listing] == ["explanation"]

    served = client.get(f"/incidents/{incident.incident_id}/evidence/{record.evidence_id}")
    assert served.status_code == 200
    assert served.headers[INTEGRITY_HEADER] == "ok"
    assert json.loads(served.content) == {"hello": "evidence"}

    # Corrupt the file: it is still served, with the warning, never silently.
    (directory / "explanation.json").write_bytes(b'{"hello": "tampered"}')
    again = client.get(f"/incidents/{incident.incident_id}/evidence/{record.evidence_id}")
    assert again.status_code == 200
    assert again.headers[INTEGRITY_HEADER] == "mismatch"


def test_a_deleted_evidence_file_is_a_410_not_a_500(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, ulids, incident = seeded
    _client, _response2, _root = api
    record = Evidence(
        evidence_id=ulids.new(),
        incident_id=incident.incident_id,
        kind=EvidenceKind.CLIP_PRE,
        relative_path=f"{CAMERA}/2026/03/01/{incident.incident_id}/clip_pre.mp4",
        sha256="0" * 64,
        size_bytes=10,
        media_type="video/mp4",
        captured_wall_utc=WALL,
        span_ms=None,
        expires_wall_utc=None,
    )
    response.evidence.add_all([record])
    reply = client.get(f"/incidents/{incident.incident_id}/evidence/{record.evidence_id}")
    assert reply.status_code == 410
    assert "retention" in reply.json()["detail"]


def test_evidence_of_another_incident_is_not_served(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, response, ulids, _first = seeded
    _other = _seed(response, ulids, _incident_other(ulids))
    reply = client.get(f"/incidents/{_other.incident_id}/evidence/{ulids.new()}")
    assert reply.status_code == 404


# ---------------------------------------------------------------- feeds and mutes


def test_the_rejection_feed_is_empty_but_present(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _root = api
    assert client.get("/events/candidates").json() == []
    assert client.get("/events/candidates/reasons").json() == []


def test_an_unknown_candidate_outcome_is_rejected(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _root = api
    reply = client.get("/events/candidates", params={"outcome": "maybe"})
    assert reply.status_code == 422


def test_the_timeline_carries_both_series(
    seeded: tuple[httpx.Client, ResponseStack, UlidFactory, Incident],
) -> None:
    client, _response, _ulids, _first = seeded
    body = client.get("/events/timeline", params={"hours": 24 * 7}).json()
    assert body["since_utc"] < body["until_utc"]
    assert "incidents" in body
    assert "health_events" in body


def test_a_mute_is_created_listed_with_a_countdown_and_revoked(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _root = api
    created = client.post(
        "/mutes",
        json={"duration_s": 600, "reason": "contractors until 5", "camera_id": CAMERA},
    )
    assert created.status_code == 201
    mute = created.json()
    assert mute["remaining_s"] > 0.0, "a mute is never silent: it shows a countdown"
    assert mute["active"] is True
    assert mute["reason"] == "contractors until 5"

    listed = client.get("/mutes").json()
    assert [m["mute_id"] for m in listed] == [mute["mute_id"]]

    assert client.delete(f"/mutes/{mute['mute_id']}").status_code == 204
    assert client.get("/mutes").json() == []
    assert client.delete(f"/mutes/{mute['mute_id']}").status_code == 404


def test_a_mute_needs_a_reason_and_a_bounded_duration(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _root = api
    assert client.post("/mutes", json={"duration_s": 60}).status_code == 422
    assert client.post("/mutes", json={"duration_s": 0, "reason": "x"}).status_code == 422
    assert client.post("/mutes", json={"duration_s": 10**9, "reason": "forever"}).status_code == 422
    assert (
        client.post("/mutes", json={"duration_s": 60, "reason": "x", "nonsense": 1}).status_code
        == 422
    )
