"""Camera, zone and credential administration, and the controlled restart that applies them.

**Changes apply on a controlled engine restart, not live.** The engine builds its cameras, zone
capabilities and module bindings once, at start (docs/DESKTOP-AUDIT-AND-PLAN.md finding F2), and
doing that at run time is a rewrite of the pipeline's assembly (ADR 0017). So the honest contract
is: a change is *saved at once* and *takes effect after a restart*, the console says so, and
`GET /config/pending` reports exactly what differs between the files and the running engine.

Everything here needs the CONFIGURE permission except reading the lists. Every change is audited
with its before and after, and no secret is ever returned, logged or audited.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from vigil.api.live_apply import apply_zone_edits
from vigil.api.live_apply import file_hash as _hash
from vigil.api.session import audit_event, requires
from vigil.auth.roles import Permission
from vigil.config import writer
from vigil.config.schema.camera import CameraConfig, SourceConfig
from vigil.config.schema.zones import ZoneSetConfig
from vigil.config.zones import zone_file_path
from vigil.core.errors import ConfigError, SourceError
from vigil.ingest import credentials as creds
from vigil.ingest.probe import ProbeResult, list_devices, probe_source
from vigil.pipeline.app import Application
from vigil.pipeline.cameras import camera_from_config

_VIEW = requires(Permission.VIEW)
_CONFIGURE = requires(Permission.CONFIGURE)


class AdminCamera(BaseModel):
    config: CameraConfig
    editable: bool  # False when it is defined somewhere other than its own file in cameras/
    state: str | None  # the running camera's state, or None when it is not running yet
    pending: Literal["added", "changed", "removed", None]
    credential_stored: bool | None  # None: this camera uses no stored credential


class PendingChange(BaseModel):
    kind: Literal["camera", "zones"]
    target: str
    change: Literal["added", "changed", "removed"]


class PendingOut(BaseModel):
    pending: bool
    changes: list[PendingChange]
    can_restart: bool  # False when run without a supervisor: the engine would exit and stay down


class ProbeOut(BaseModel):
    ok: bool
    message: str
    width_px: int | None
    height_px: int | None
    fps: float | None
    backend: str | None
    detail: str | None
    timed_out: bool

    @classmethod
    def of(cls, r: ProbeResult) -> ProbeOut:
        return cls(**{k: getattr(r, k) for k in cls.model_fields})


class DeviceOut(BaseModel):
    index: int
    name: str
    backend: str


class DevicesOut(BaseModel):
    available: bool
    reason: str | None
    devices: list[DeviceOut]


class ZonesOut(BaseModel):
    zones_ref: str | None
    zones: ZoneSetConfig
    pending: bool
    editable: bool


class LiveSkipped(BaseModel):
    target: str
    reason: str


class LiveApplyOut(BaseModel):
    applied: list[str]  # what took effect now, without a restart
    needs_restart: list[LiveSkipped]  # what could not, and why
    pending: PendingOut  # everything still waiting on a restart


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(max_length=256)


class CredentialOut(BaseModel):
    ref: str
    stored: bool


def build_router(*, application: Application, restart: Callable[[], None] | None) -> APIRouter:
    router = APIRouter(prefix="/config", tags=["configuration"])
    config_dir = application.paths.config_dir
    running = {c.camera_id: c for c in application.settings.cameras}
    # What each zone file held when this engine started, so "changed" is a fact about the files.
    zones_at_start: dict[str, str | None] = {}
    for cam in application.settings.cameras:
        ref = cam.zones_ref
        if ref is not None:
            zones_at_start[ref] = _hash(zone_file_path(config_dir, ref))

    def refuse(exc: ConfigError) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": exc.message, "issues": list(exc.issues)},
        )

    def states() -> dict[str, str]:
        return {h.camera_id: h.state.value for h in application.camera_health()}

    def stored(cam: CameraConfig) -> bool | None:
        ref = cam.source.credential_ref
        if ref is None:
            return None
        try:
            return creds.has_credentials(ref)
        except SourceError:
            return False  # no keyring on this machine: nothing can be stored

    def admin_camera(cam: CameraConfig, *, editable: bool) -> AdminCamera:
        before = running.get(cam.camera_id)
        change: Literal["added", "changed", "removed", None]
        if before is None:
            change = "added"
        elif before != cam:
            change = "changed"
        else:
            change = None
        return AdminCamera(
            config=cam,
            editable=editable,
            state=states().get(cam.camera_id),
            pending=change,
            credential_stored=stored(cam),
        )

    def on_disk() -> dict[str, CameraConfig]:
        found: dict[str, CameraConfig] = {}
        for camera_id in writer.list_camera_files(config_dir):
            try:
                cam = writer.read_camera(config_dir, camera_id)
            except ConfigError:
                continue  # an unreadable file is the engine's problem to report at start
            if cam is not None:
                found[cam.camera_id] = cam
        return found

    def pending_changes() -> list[PendingChange]:
        files = on_disk()
        out: list[PendingChange] = []
        for camera_id, cam in files.items():
            before = running.get(camera_id)
            if before is None:
                out.append(PendingChange(kind="camera", target=camera_id, change="added"))
            elif before != cam:
                out.append(PendingChange(kind="camera", target=camera_id, change="changed"))
        for camera_id in running:
            # Removed only if it was a file-backed camera that is now gone; an inline camera has
            # no file to lose.
            if camera_id not in files and camera_id in started_files:
                out.append(PendingChange(kind="camera", target=camera_id, change="removed"))
        for ref in set(zones_at_start) | set(writer.zone_refs(config_dir)):
            now = _hash(zone_file_path(config_dir, ref))
            if now != zones_at_start.get(ref):
                change = (
                    "added"
                    if ref not in zones_at_start
                    else ("removed" if now is None else "changed")
                )
                out.append(PendingChange(kind="zones", target=ref, change=change))
        return out

    started_files = set(writer.list_camera_files(config_dir))

    # ------------------------------------------------------------ cameras

    @router.get("/cameras", response_model=list[AdminCamera], dependencies=[_VIEW])
    def cameras() -> list[AdminCamera]:
        files = on_disk()
        merged: dict[str, tuple[CameraConfig, bool]] = {
            cid: (cam, False) for cid, cam in running.items()
        }
        for cid, cam in files.items():
            merged[cid] = (cam, True)
        for cid in running:
            if cid in started_files and cid not in files:
                merged.pop(cid, None)  # deleted since start: shown by /pending, not as a camera
        return [admin_camera(cam, editable=editable) for cam, editable in merged.values()]

    @router.post(
        "/cameras",
        response_model=AdminCamera,
        status_code=status.HTTP_201_CREATED,
        dependencies=[_CONFIGURE],
    )
    def add_camera(request: Request, camera: Annotated[CameraConfig, Body()]) -> AdminCamera:
        if camera.camera_id in running or camera.camera_id in on_disk():
            raise HTTPException(status.HTTP_409_CONFLICT, "A camera with that id already exists.")
        writer.write_camera(config_dir, camera)
        audit_event(request, "camera.added", target=camera.camera_id, detail=_summary(camera))
        return admin_camera(camera, editable=True)

    @router.put("/cameras/{camera_id}", response_model=AdminCamera, dependencies=[_CONFIGURE])
    def update_camera(
        request: Request, camera_id: str, camera: Annotated[CameraConfig, Body()]
    ) -> AdminCamera:
        if camera.camera_id != camera_id:
            raise HTTPException(422, "The id in the body must match the id in the path.")
        existing = writer.read_camera(config_dir, camera_id)
        if existing is None:
            if camera_id in running:
                raise HTTPException(
                    409, "This camera is defined outside the console; edit its file by hand."
                )
            raise HTTPException(404, "No such camera.")
        writer.write_camera(config_dir, camera)
        audit_event(
            request,
            "camera.changed",
            target=camera_id,
            detail={"before": _summary(existing), "after": _summary(camera)},
        )
        return admin_camera(camera, editable=True)

    @router.delete(
        "/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_CONFIGURE]
    )
    def remove_camera(request: Request, camera_id: str) -> Response:
        existing = writer.read_camera(config_dir, camera_id)
        if existing is None:
            raise HTTPException(
                404, "No such camera file. An inline camera must be edited by hand."
            )
        writer.delete_camera(config_dir, camera_id)
        audit_event(request, "camera.removed", target=camera_id, detail=_summary(existing))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/cameras/test", response_model=ProbeOut, dependencies=[_CONFIGURE])
    def test_camera(request: Request, source: Annotated[SourceConfig, Body()]) -> ProbeOut:
        """Open the source, read one frame, close it. Does not save anything."""
        if source.kind == "webcam":
            for cam in running.values():
                if (
                    cam.source.kind == "webcam"
                    and cam.source.device_index == source.device_index
                    and cam.enabled
                ):
                    return ProbeOut.of(
                        ProbeResult(
                            False,
                            f"This device is already in use by the running camera "
                            f"{cam.camera_id!r}. It cannot be opened twice.",
                        )
                    )
        spec = camera_from_config(CameraConfig(camera_id="probe", source=source)).source
        result = probe_source(spec)
        audit_event(
            request,
            "camera.tested",
            detail={"kind": source.kind, "ok": result.ok, "timed_out": result.timed_out},
        )
        return ProbeOut.of(result)

    @router.get("/devices", response_model=DevicesOut, dependencies=[_CONFIGURE])
    def devices() -> DevicesOut:
        found = list_devices()
        return DevicesOut(
            available=found.available,
            reason=found.reason,
            devices=[
                DeviceOut(index=d.index, name=d.name, backend=d.backend) for d in found.devices
            ],
        )

    # ------------------------------------------------------------ zones

    def zones_for(camera_id: str) -> tuple[CameraConfig, str, bool]:
        file_cam = writer.read_camera(config_dir, camera_id)
        cam = file_cam or running.get(camera_id)
        if cam is None:
            raise HTTPException(404, "No such camera.")
        return cam, cam.zones_ref or camera_id, file_cam is not None

    @router.get("/cameras/{camera_id}/zones", response_model=ZonesOut, dependencies=[_VIEW])
    def get_zones(camera_id: str) -> ZonesOut:
        cam, ref, editable = zones_for(camera_id)
        path = zone_file_path(config_dir, ref)
        try:
            current = (
                ZoneSetConfig.model_validate(_load_yaml(path))
                if path.is_file()
                else ZoneSetConfig()
            )
        except ConfigError as exc:
            raise refuse(exc) from None
        now = _hash(path)
        return ZonesOut(
            zones_ref=cam.zones_ref,
            zones=current,
            pending=now != zones_at_start.get(ref),
            editable=editable,
        )

    @router.put("/cameras/{camera_id}/zones", response_model=ZonesOut, dependencies=[_CONFIGURE])
    def put_zones(
        request: Request, camera_id: str, zones: Annotated[ZoneSetConfig, Body()]
    ) -> ZonesOut:
        cam, ref, editable = zones_for(camera_id)
        if not editable:
            raise HTTPException(409, "This camera is defined outside the console; edit it by hand.")
        before = _hash(zone_file_path(config_dir, ref))
        writer.write_zone_set(config_dir, ref, zones)
        if cam.zones_ref is None:
            cam = cam.model_copy(update={"zones_ref": ref})
            writer.write_camera(config_dir, cam)  # point the camera at the file it now has
        audit_event(
            request,
            "zones.saved",
            target=camera_id,
            detail={"zones": [z.zone_id for z in zones.zones], "had_file": before is not None},
        )
        return ZonesOut(
            zones_ref=ref,
            zones=zones,
            pending=_hash(zone_file_path(config_dir, ref)) != zones_at_start.get(ref),
            editable=True,
        )

    # ------------------------------------------------------------ credentials

    @router.get("/credentials/{ref}", response_model=CredentialOut, dependencies=[_CONFIGURE])
    def credential_status(ref: str) -> CredentialOut:
        _check_ref(ref)
        try:
            return CredentialOut(ref=ref, stored=creds.has_credentials(ref))
        except SourceError as exc:
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, exc.message) from None

    @router.put("/credentials/{ref}", response_model=CredentialOut, dependencies=[_CONFIGURE])
    def set_credential(
        request: Request, ref: str, body: Annotated[CredentialIn, Body()]
    ) -> CredentialOut:
        _check_ref(ref)
        try:
            creds.store_credentials(ref, body.username, body.password)
        except SourceError as exc:
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, exc.message) from None
        audit_event(request, "credential.stored", target=ref)  # the name only, never the pair
        return CredentialOut(ref=ref, stored=True)

    @router.delete(
        "/credentials/{ref}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_CONFIGURE]
    )
    def delete_credential(request: Request, ref: str) -> Response:
        _check_ref(ref)
        try:
            creds.delete_credentials(ref)
        except SourceError as exc:
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, exc.message) from None
        audit_event(request, "credential.deleted", target=ref)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------ applying

    @router.get("/pending", response_model=PendingOut, dependencies=[_VIEW])
    def pending() -> PendingOut:
        changes = pending_changes()
        return PendingOut(pending=bool(changes), changes=changes, can_restart=restart is not None)

    @router.post("/apply-live", response_model=LiveApplyOut, dependencies=[_CONFIGURE])
    def apply_live(request: Request) -> LiveApplyOut:
        """Apply what can take effect now (zone and line edits) and report what cannot."""
        outcome = apply_zone_edits(application, config_dir, zones_at_start)
        audit_event(
            request,
            "config.applied_live",
            detail={"applied": list(outcome.applied), "skipped": len(outcome.skipped)},
        )
        changes = pending_changes()
        return LiveApplyOut(
            applied=list(outcome.applied),
            needs_restart=[LiveSkipped(target=s.target, reason=s.reason) for s in outcome.skipped],
            pending=PendingOut(
                pending=bool(changes), changes=changes, can_restart=restart is not None
            ),
        )

    @router.post(
        "/apply",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=PendingOut,
        dependencies=[_CONFIGURE],
    )
    def apply(request: Request) -> PendingOut:
        """Restart the engine so it loads what is on disk. Monitoring stops for a few seconds."""
        if restart is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This engine is not running under the desktop app, so it cannot restart itself. "
                "Stop and start it to apply the changes.",
            )
        changes = pending_changes()
        audit_event(request, "config.apply_requested", detail={"changes": len(changes)})
        restart()
        return PendingOut(pending=bool(changes), changes=changes, can_restart=True)

    return router


def _check_ref(ref: str) -> None:
    if not creds.REF_PATTERN.match(ref):
        raise HTTPException(422, "A credential name is a short slug such as 'gate-camera'.")


def _summary(cam: CameraConfig) -> dict[str, Any]:
    """What an audit row records about a camera: enough to see what changed, never a secret."""
    src = cam.source
    return {
        "name": cam.display_name,
        "kind": src.kind,
        "enabled": cam.enabled,
        "priority": cam.priority,
        "target_inference_fps": cam.target_inference_fps,
        "zones_ref": cam.zones_ref,
        "credential_ref": src.credential_ref,
    }


def _load_yaml(path: Path) -> object:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path.name}", issues=[str(exc)]) from exc
