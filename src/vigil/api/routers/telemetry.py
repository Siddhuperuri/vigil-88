"""The live channel (08 §5): one WebSocket that pushes what changed.

It carries two kinds of message and nothing the REST API cannot answer, so a client that misses a
message loses a notification, never the truth:

  * `snapshot`  -- application state, health, every camera, and system load. Sent when it changes
                   and at least every `HEARTBEAT_S`, so a console can tell a quiet engine from a
                   dead connection (a frozen clock is the fastest way to spot a hung UI, 08 §3).
  * `incident`  -- an incident appeared or changed state or severity. It names the incident and its
                   summary; the console fetches the detail through the guarded REST route.

Pushes are coalesced: the engine is polled at `POLL_S` and only a difference is sent (08 §5 rule 4).
Nothing here touches a pipeline lock beyond the short critical sections the REST routes use.

Authorisation is the same as everywhere else: the upgrade is refused before it is accepted unless
the session may VIEW, and an open socket is closed the moment its session expires, locks, or is
revoked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from vigil.api.schemas import CameraOut, SystemOut
from vigil.api.schemas_incidents import IncidentSummaryOut
from vigil.api.session import SESSION_COOKIE, AccountsGuard, Guard, requires_socket
from vigil.auth.roles import Permission
from vigil.persistence.queries import IncidentFilter
from vigil.persistence.repositories.evidence import EvidenceRepository
from vigil.persistence.repositories.incidents import IncidentRepository
from vigil.pipeline.app import Application

POLL_S = 0.5
HEARTBEAT_S = 2.0
WATCHED_INCIDENTS = 50  # the newest incidents are the ones whose changes matter live

CLOSE_SIGNED_OUT = 4401  # the session ended: expired, logged out, or revoked
CLOSE_LOCKED = 4423  # the console was locked; monitoring continues, the socket does not


def _snapshot(application: Application) -> dict[str, Any]:
    snap = application.snapshot()
    return {
        "kind": "snapshot",
        "state": snap.state.value,
        "health": {"level": snap.health.level.value, "issues": list(snap.health.issues)},
        "uptime_ms": snap.uptime_ms,
        "cameras": [CameraOut.of(c).model_dump(mode="json") for c in snap.cameras],
        "system": None
        if snap.system is None
        else SystemOut.of(snap.system).model_dump(mode="json"),
    }


def build_router(
    *,
    application: Application,
    guard: Guard,
    incidents: IncidentRepository | None,
    evidence: EvidenceRepository | None,
) -> APIRouter:
    router = APIRouter(tags=["telemetry"])

    def watched() -> dict[str, tuple[str, int, IncidentSummaryOut]]:
        """The newest incidents keyed by id, with what a change would show up in."""
        if incidents is None or evidence is None:
            return {}
        found: dict[str, tuple[str, int, IncidentSummaryOut]] = {}
        for item in incidents.page(IncidentFilter(), limit=WATCHED_INCIDENTS).incidents:
            summary = IncidentSummaryOut.of(
                item,
                evidence_count=len(evidence.ids_for(item.incident_id)),
                evidence_status=incidents.evidence_status(item.incident_id),
            )
            found[item.incident_id] = (item.status.value, int(item.severity), summary)
        return found

    def still_allowed(websocket: WebSocket) -> int | None:
        """A close code if this socket may no longer stay open, else None."""
        if not isinstance(guard, AccountsGuard):
            return None
        who = guard.principal_for(websocket, websocket.cookies.get(SESSION_COOKIE))
        if who is None:
            return CLOSE_SIGNED_OUT
        return CLOSE_LOCKED if who.locked else None

    @router.websocket("/telemetry", dependencies=[requires_socket(Permission.VIEW)])
    async def telemetry(websocket: WebSocket) -> None:
        await websocket.accept()
        known = await run_in_threadpool(watched)  # the baseline: only later changes are pushed
        last_sent = ""
        loops_since_send = 0.0
        try:
            while True:
                code = await run_in_threadpool(still_allowed, websocket)
                if code is not None:
                    await websocket.close(code=code)
                    return
                body = json.dumps(_snapshot(application), sort_keys=True)
                if body != last_sent or loops_since_send >= HEARTBEAT_S:
                    await websocket.send_text(body)
                    last_sent, loops_since_send = body, 0.0
                current = await run_in_threadpool(watched)
                for incident_id, (status, severity, summary) in current.items():
                    before = known.get(incident_id)
                    if before is None or before[:2] != (status, severity):
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "kind": "incident",
                                    "change": "new" if before is None else "updated",
                                    "incident": summary.model_dump(mode="json"),
                                }
                            )
                        )
                known = current
                try:
                    # Waiting on the client doubles as the poll delay and notices a disconnect.
                    await asyncio.wait_for(websocket.receive_text(), timeout=POLL_S)
                except TimeoutError:
                    loops_since_send += POLL_S
        except WebSocketDisconnect:
            return

    return router
