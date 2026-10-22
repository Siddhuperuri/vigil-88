"""Per-camera module availability, and alert-channel status (04 §4, 08 §8).

This is the endpoint that makes "no fake functionality" visible rather than merely true. The
console renders exactly what it is told here:

  * a module that **will run** is offered;
  * a module that is UNAVAILABLE is listed greyed, with the missing capability and the fix --
    *"needs ZONES: set cameras.<id>.zones_ref ..."* -- rather than silently absent;
  * a module that is DEGRADED is offered with its caveats, which also travel onto every
    incident it produces.

The console never works availability out for itself. If it did, two places would decide what a
camera can do, and one of them would eventually be wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fastapi import APIRouter, HTTPException

from vigil.api.schemas_incidents import ChannelStatusOut, ModuleAvailabilityOut
from vigil.events.binding import BindingReport
from vigil.response.channels.base import ChannelStatus


def _module_out(entry: object) -> ModuleAvailabilityOut:
    from vigil.events.registry import ModuleAvailability

    assert isinstance(entry, ModuleAvailability)
    return ModuleAvailabilityOut(
        module_id=entry.module_id,
        event_type=entry.event_type.value,
        version=entry.version,
        enabled=entry.enabled,
        status=entry.status.value,
        will_run=entry.will_run,
        requires=sorted(c.value for c in entry.requires),
        missing_required=sorted(c.value for c in entry.missing_required),
        missing_optional=sorted(c.value for c in entry.missing_optional),
        reason=entry.reason,
        description=entry.description,
        caveats=list(entry.caveats),
    )


def build_router(
    *,
    bindings: Mapping[str, BindingReport],
    channel_status: Sequence[ChannelStatus],
    zone_issues: Mapping[str, str],
) -> APIRouter:
    router = APIRouter(tags=["modules"])

    @router.get("/cameras/{camera_id}/modules", response_model=list[ModuleAvailabilityOut])
    def camera_modules(camera_id: str) -> list[ModuleAvailabilityOut]:
        report = bindings.get(camera_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"unknown camera {camera_id!r}")
        return [_module_out(a) for a in report.availability]

    @router.get("/cameras/{camera_id}/zones/status", response_model=dict[str, str | None])
    def zone_status(camera_id: str) -> dict[str, str | None]:
        """Why a camera has no zones, when it asked for some. An empty issue means they loaded."""
        if camera_id not in bindings:
            raise HTTPException(status_code=404, detail=f"unknown camera {camera_id!r}")
        return {"camera_id": camera_id, "issue": zone_issues.get(camera_id)}

    @router.get("/system/channels", response_model=list[ChannelStatusOut])
    def channels() -> list[ChannelStatusOut]:
        """An unavailable channel is named with its reason, never quietly omitted (08 §8)."""
        return [
            ChannelStatusOut(channel_id=s.channel_id, available=s.available, reason=s.reason)
            for s in channel_status
        ]

    return router
