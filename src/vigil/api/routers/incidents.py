"""Incident listing, detail, evidence and operator actions (08 §11).

Three rules this router exists to honour.

**The UI never queries the database and never decides anything.** Filters arrive as query
parameters, are validated here, and become an `IncidentFilter` that the repository turns into
one parameterised statement. Nothing a caller sends reaches SQL as text.

**No decorative controls.** Every detail response carries `permissions`, so the console can
disable an action and say why rather than offering a button that returns an error (08 §8). An
action the lifecycle forbids answers 409 with the reason, not 500.

**Evidence integrity is checked on read, and a mismatch is reported rather than hidden.** A
stored path is still input: it is re-resolved against the evidence root before any file is
opened, so a tampered row cannot become an arbitrary file read (05 §6, 01 §10).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from vigil.api.schemas_incidents import (
    EvidenceOut,
    ExplanationOut,
    IncidentDetailOut,
    IncidentPageOut,
    IncidentSummaryOut,
    evidence_out,
    model_out,
    permissions_out,
    system_out,
    transitions_out,
)
from vigil.core.errors import PersistenceError
from vigil.domain.enums import EventType, IncidentStatus, Severity
from vigil.domain.incident import Incident
from vigil.persistence.queries import (
    DEFAULT_SORT,
    SORT_COLUMNS,
    IncidentFilter,
    clamp_page_size,
)
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.response.evidence import verify_file
from vigil.response.incident_manager import TransitionRefused
from vigil.response.operator_actions import OperatorActions
from vigil.response.paths import resolve_relative

# PEP 695 generics need 3.12; this project targets 3.11 (D-001).
_T = TypeVar("_T")

CURSOR_SEPARATOR = "~"
DEFAULT_PAGE_SIZE = 50
INTEGRITY_HEADER = "X-Evidence-Integrity"


def _parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    if cursor is None:
        return None
    sort_value, separator, incident_id = cursor.partition(CURSOR_SEPARATOR)
    if not separator or not sort_value or not incident_id:
        raise HTTPException(status_code=422, detail="malformed cursor")
    return (sort_value, incident_id)


def _render_cursor(cursor: tuple[str, str] | None) -> str | None:
    return None if cursor is None else f"{cursor[0]}{CURSOR_SEPARATOR}{cursor[1]}"


def _enum_list(raw: str | None, kind: Callable[[str], _T], label: str) -> tuple[_T, ...]:
    """Parse a comma-separated enum list, rejecting an unknown member by name."""
    if not raw:
        return ()
    out: list[_T] = []
    for piece in raw.split(","):
        token = piece.strip()
        if not token:
            continue
        try:
            out.append(kind(token))
        except (KeyError, ValueError):
            raise HTTPException(status_code=422, detail=f"unknown {label}: {token!r}") from None
    return tuple(out)


def _severity(raw: str | None) -> Severity | None:
    if not raw:
        return None
    try:
        return Severity[raw.strip().upper()]
    except KeyError:
        raise HTTPException(status_code=422, detail=f"unknown severity: {raw!r}") from None


def build_router(
    *,
    incidents: IncidentRepository,
    evidence: EvidenceRepository,
    actions: OperatorActions,
    evidence_root: Path,
    max_page_size: int,
) -> APIRouter:
    router = APIRouter(prefix="/incidents", tags=["incidents"])

    def _require(incident_id: str) -> Incident:
        try:
            incident = incidents.get(incident_id)
        except PersistenceError as exc:
            # A row this build cannot read is reported as such rather than 500-ing the list.
            raise HTTPException(status_code=500, detail=exc.message) from None
        if incident is None:
            raise HTTPException(status_code=404, detail=f"unknown incident {incident_id}")
        return incident

    def _summary(incident: Incident) -> IncidentSummaryOut:
        return IncidentSummaryOut.of(
            incident,
            evidence_count=len(evidence.ids_for(incident.incident_id)),
            evidence_status=incidents.evidence_status(incident.incident_id),
        )

    @router.get("", response_model=IncidentPageOut)
    def list_incidents(
        camera_id: Annotated[str | None, Query(max_length=400)] = None,
        event_type: Annotated[str | None, Query(max_length=400)] = None,
        incident_status: Annotated[str | None, Query(alias="status", max_length=200)] = None,
        min_severity: Annotated[str | None, Query(max_length=20)] = None,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        zone_id: Annotated[str | None, Query(max_length=40)] = None,
        search: Annotated[str | None, Query(max_length=200)] = None,
        open_only: Annotated[bool, Query()] = False,
        sort: Annotated[str, Query(max_length=40)] = DEFAULT_SORT,
        descending: Annotated[bool, Query()] = True,
        limit: Annotated[int | None, Query(ge=1)] = None,
        cursor: Annotated[str | None, Query(max_length=120)] = None,
    ) -> IncidentPageOut:
        if sort not in SORT_COLUMNS:
            known = ", ".join(sorted(SORT_COLUMNS))
            raise HTTPException(status_code=422, detail=f"unknown sort {sort!r}; known: {known}")
        try:
            filters = IncidentFilter(
                camera_ids=_enum_list(camera_id, str, "camera"),
                event_types=_enum_list(event_type, EventType, "event type"),
                statuses=_enum_list(incident_status, IncidentStatus, "status"),
                min_severity=_severity(min_severity),
                since_wall_utc=since,
                until_wall_utc=until,
                zone_id=zone_id,
                search=search,
                open_only=open_only,
            )
        except PersistenceError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from None

        size = clamp_page_size(limit, default=DEFAULT_PAGE_SIZE, maximum=max_page_size)
        page = incidents.page(
            filters,
            limit=size,
            cursor=_parse_cursor(cursor),
            sort=sort,
            descending=descending,
        )
        counts = evidence.counts_by_incident([i.incident_id for i in page.incidents])
        return IncidentPageOut(
            incidents=[
                IncidentSummaryOut.of(
                    i,
                    evidence_count=counts.get(i.incident_id, 0),
                    evidence_status=incidents.evidence_status(i.incident_id),
                )
                for i in page.incidents
            ],
            total=page.total,
            next_cursor=_render_cursor(page.next_cursor),
            page_size=size,
        )

    @router.get("/{incident_id}", response_model=IncidentDetailOut)
    def incident_detail(incident_id: str) -> IncidentDetailOut:
        incident = _require(incident_id)
        records = evidence.for_incident(incident_id)
        return IncidentDetailOut(
            incident=IncidentSummaryOut.of(
                incident,
                evidence_count=len(records),
                evidence_status=incidents.evidence_status(incident_id),
            ),
            explanation=ExplanationOut.of(incident),
            transitions=transitions_out(incident.transitions),
            evidence=evidence_out(records),
            operator_note=incident.operator_note,
            involved_track_ids=sorted(incident.involved_track_ids),
            tracker_epoch=incident.tracker_epoch,
            model=model_out(incident),
            system=system_out(incident),
            available_transitions=[s.value for s in actions.available_transitions(incident_id)],
            permissions=permissions_out(actions.permissions(incident_id)),
        )

    @router.get("/{incident_id}/evidence", response_model=list[EvidenceOut])
    def incident_evidence(incident_id: str) -> list[EvidenceOut]:
        _require(incident_id)
        return evidence_out(evidence.for_incident(incident_id))

    @router.get("/{incident_id}/evidence/{evidence_id}")
    def evidence_file(incident_id: str, evidence_id: str) -> FileResponse:
        """Serve one evidence file, path-checked and hash-verified.

        A hash mismatch is served with `X-Evidence-Integrity: mismatch` rather than suppressed:
        the operator needs the file *and* the warning, and silently withholding either would be
        worse than saying so (05 §6).
        """
        _require(incident_id)
        record = evidence.get(evidence_id)
        if record is None or record.incident_id != incident_id:
            raise HTTPException(status_code=404, detail="unknown evidence")
        try:
            path = resolve_relative(evidence_root, record.relative_path)
        except PersistenceError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from None
        if not path.is_file():
            raise HTTPException(
                status_code=410,
                detail="the evidence file is no longer on disk (deleted by retention, or lost)",
            )
        ok = verify_file(path, record.sha256)
        return FileResponse(
            path,
            media_type=record.media_type,
            filename=path.name,
            headers={
                INTEGRITY_HEADER: "ok" if ok else "mismatch",
                "Cache-Control": "private, max-age=60",
            },
        )

    # ---------------------------------------------------------------- operator actions

    def _act(incident_id: str, action: str, body: dict[str, str] | None) -> IncidentDetailOut:
        _require(incident_id)
        allowed, reason = actions.can(incident_id, action)
        if not allowed:
            # 409, not 500: the request was well formed and the lifecycle simply says no.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
        text = (body or {}).get("reason") or (body or {}).get("note")
        try:
            if action == "acknowledge":
                actions.acknowledge(incident_id, note=text)
            elif action == "dismiss":
                actions.dismiss(incident_id, reason=text)
            elif action == "escalate":
                actions.escalate(incident_id, reason=text)
            elif action == "annotate":
                actions.annotate(incident_id, text)
        except TransitionRefused as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from None
        except PersistenceError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from None
        return incident_detail(incident_id)

    @router.post("/{incident_id}/acknowledge", response_model=IncidentDetailOut)
    def acknowledge(incident_id: str, body: dict[str, str] | None = None) -> IncidentDetailOut:
        return _act(incident_id, "acknowledge", body)

    @router.post("/{incident_id}/dismiss", response_model=IncidentDetailOut)
    def dismiss(incident_id: str, body: dict[str, str] | None = None) -> IncidentDetailOut:
        return _act(incident_id, "dismiss", body)

    @router.post("/{incident_id}/escalate", response_model=IncidentDetailOut)
    def escalate(incident_id: str, body: dict[str, str] | None = None) -> IncidentDetailOut:
        return _act(incident_id, "escalate", body)

    @router.post("/{incident_id}/annotate", response_model=IncidentDetailOut)
    def annotate(incident_id: str, body: dict[str, str] | None = None) -> IncidentDetailOut:
        return _act(incident_id, "annotate", body)

    @router.delete("/{incident_id}/annotate", status_code=status.HTTP_204_NO_CONTENT)
    def clear_note(incident_id: str) -> Response:
        _require(incident_id)
        actions.annotate(incident_id, None)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
