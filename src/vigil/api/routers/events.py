"""The rejection feed and the timeline (08 §3, §5, §11).

**Rejections are a first-class view.** During tuning, watching candidates stream past with the
validator that refused them is the most informative thing in the system, and it is how an
over-aggressive validator gets caught before it silently eats real events (06 §6). Without this
view you only ever see what got through, and you are tuning blind.

**The timeline puts incidents and camera-health events on one axis**, because
*"the camera was reconnecting when that fired"* answers a large fraction of "why did this fire"
questions. Both series are bounded by an explicit window and a row cap, so a client cannot ask
for more than it can draw (08 §10).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from vigil.api.schemas_incidents import (
    CandidateOut,
    HealthEventOut,
    IncidentSummaryOut,
    TimelineOut,
)
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.domain.enums import CandidateOutcome
from vigil.persistence.queries import IncidentFilter, clamp_page_size
from vigil.persistence.repositories.events import CandidateRepository, HealthEventRepository
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository

DEFAULT_LIMIT = 100
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 24 * 90


def build_router(
    *,
    candidates: CandidateRepository,
    health: HealthEventRepository,
    incidents: IncidentRepository,
    evidence: EvidenceRepository,
    clock: Clock,
    max_page_size: int,
) -> APIRouter:
    router = APIRouter(prefix="/events", tags=["events"])

    @router.get("/candidates", response_model=list[CandidateOut])
    def rejection_feed(
        camera_id: Annotated[str | None, Query(max_length=40)] = None,
        outcome: Annotated[str | None, Query(max_length=200)] = None,
        hours: Annotated[int, Query(ge=1, le=MAX_WINDOW_HOURS)] = DEFAULT_WINDOW_HOURS,
        limit: Annotated[int | None, Query(ge=1)] = None,
    ) -> list[CandidateOut]:
        outcomes: list[CandidateOutcome] = []
        for token in (outcome or "").split(","):
            name = token.strip()
            if not name:
                continue
            try:
                outcomes.append(CandidateOutcome(name))
            except ValueError:
                raise HTTPException(
                    status_code=422, detail=f"unknown candidate outcome: {name!r}"
                ) from None
        size = clamp_page_size(limit, default=DEFAULT_LIMIT, maximum=max_page_size)
        try:
            rows = candidates.recent(
                limit=size,
                camera_id=camera_id,
                outcomes=tuple(outcomes),
                since_wall_utc=clock.wall_utc() - timedelta(hours=hours),
            )
        except PersistenceError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from None
        return [CandidateOut.of(r) for r in rows]

    @router.get("/candidates/reasons", response_model=list[dict[str, str | int]])
    def rejection_reasons(
        hours: Annotated[int, Query(ge=1, le=MAX_WINDOW_HOURS)] = DEFAULT_WINDOW_HOURS,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> list[dict[str, str | int]]:
        """Which validators are rejecting the most. The tuning input, aggregated in SQL."""
        since = clock.wall_utc() - timedelta(hours=hours)
        rows = candidates.rejection_reasons(limit=limit, since_wall_utc=since)
        return [{"reason": reason, "count": count} for reason, count in rows]

    @router.get("/timeline", response_model=TimelineOut)
    def timeline(
        hours: Annotated[int, Query(ge=1, le=MAX_WINDOW_HOURS)] = DEFAULT_WINDOW_HOURS,
        camera_id: Annotated[str | None, Query(max_length=40)] = None,
        limit: Annotated[int | None, Query(ge=1)] = None,
    ) -> TimelineOut:
        until: datetime = clock.wall_utc()
        since = until - timedelta(hours=hours)
        size = clamp_page_size(limit, default=DEFAULT_LIMIT, maximum=max_page_size)
        filters = IncidentFilter(
            camera_ids=(camera_id,) if camera_id else (),
            since_wall_utc=since,
            until_wall_utc=until,
        )
        try:
            page = incidents.page(filters, limit=size)
            events = health.recent(limit=size, camera_id=camera_id, since_wall_utc=since)
        except PersistenceError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from None
        counts = evidence.counts_by_incident([i.incident_id for i in page.incidents])
        return TimelineOut(
            since_utc=since.isoformat(),
            until_utc=until.isoformat(),
            incidents=[
                IncidentSummaryOut.of(
                    i,
                    evidence_count=counts.get(i.incident_id, 0),
                    evidence_status=incidents.evidence_status(i.incident_id),
                )
                for i in page.incidents
            ],
            health_events=[HealthEventOut.of(e) for e in events],
        )

    return router
