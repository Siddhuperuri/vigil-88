"""Creating, listing and revoking operator mutes (06 §10, 08 §11).

A mute is a real control with a real effect, so it is a real resource: it is created with a
bounded duration, it is listed with a countdown, and it is revoked rather than deleted so the
audit trail keeps who silenced what and why.

The listing is the load-bearing half. 06 §10 is explicit that mute visibility is deliberate --
*a muted camera that looks normal is how a real incident gets missed* -- so the status strip
renders `GET /mutes` with its countdown, and there is no path that applies a mute without this
endpoint being able to show it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from vigil.api.schemas_incidents import MuteOut
from vigil.core.clock import Clock
from vigil.core.errors import PersistenceError
from vigil.domain.enums import EventType
from vigil.response.mutes import MAX_MUTE_SECONDS, MuteRegistry

MAX_REASON_LENGTH = 200


class MuteRequest(BaseModel):
    """What an operator has to say to silence something."""

    model_config = ConfigDict(extra="forbid")

    duration_s: float = Field(gt=0.0, le=float(MAX_MUTE_SECONDS))
    # A reason is required, not optional: an unexplained mute is the one nobody can judge later.
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)
    camera_id: str | None = Field(default=None, max_length=40)
    zone_id: str | None = Field(default=None, max_length=40)
    event_type: EventType | None = None


def build_router(*, mutes: MuteRegistry, clock: Clock) -> APIRouter:
    router = APIRouter(prefix="/mutes", tags=["mutes"])

    def _out(record: object) -> MuteOut:
        from vigil.persistence.repositories.mutes import MuteRecord

        assert isinstance(record, MuteRecord)
        now = clock.wall_utc()
        return MuteOut.of(record, remaining_s=record.remaining_seconds(now))

    @router.get("", response_model=list[MuteOut])
    def active_mutes() -> list[MuteOut]:
        """Every mute in force now, with its countdown. What the status strip renders."""
        return [_out(r) for r in mutes.active()]

    @router.get("/history", response_model=list[MuteOut])
    def mute_history(limit: Annotated[int, 50] = 50) -> list[MuteOut]:
        """Including expired and revoked: the audit view, not the live one."""
        return [_out(r) for r in mutes.recent(limit=min(max(limit, 1), 200))]

    @router.post("", response_model=MuteOut, status_code=status.HTTP_201_CREATED)
    def create_mute(request: Annotated[MuteRequest, Body()]) -> MuteOut:
        try:
            record = mutes.create(
                duration_s=request.duration_s,
                reason=request.reason,
                camera_id=request.camera_id,
                zone_id=request.zone_id,
                event_type=request.event_type,
            )
        except (PersistenceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return _out(record)

    @router.delete("/{mute_id}", status_code=status.HTTP_204_NO_CONTENT)
    def revoke_mute(mute_id: str) -> Response:
        if not mutes.revoke(mute_id):
            raise HTTPException(status_code=404, detail="no such mute, or it was already revoked")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
