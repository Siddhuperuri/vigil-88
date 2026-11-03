"""Per-camera detection thresholds: read what is in force, change a camera's overrides.

Parameters resolve in three layers (`config/module_params.py`): the module's own defaults, then
`config/modules/<module>.yaml`, then this camera's `module_params`. This router edits only the
last layer, in the camera's own file, so a threshold changed for one camera cannot change any
other, and clearing it returns the camera to the shared value.

**Validation is the module's own.** An override is accepted only if the module binds with the
merged parameters, so the bounds an operator sees are the bounds the engine enforces, and an
unknown name is refused rather than ignored. Thresholds are read when a module is bound, at
start, so a saved change is *pending a restart*; the response says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError

from vigil.api.session import audit_event, requires
from vigil.auth.roles import Permission
from vigil.config import writer
from vigil.config.module_params import load_module_params, merge_params
from vigil.config.schema.camera import CameraConfig
from vigil.core.errors import ConfigError, VigilError
from vigil.core.protocols.module import DetectionModule, ModuleContext
from vigil.events.registry import ModuleRegistry
from vigil.observability.logging import get_logger
from vigil.pipeline.app import Application

_VIEW = requires(Permission.VIEW)
_CONFIGURE = requires(Permission.CONFIGURE)
MAX_OVERRIDES = 64


class TuningParam(BaseModel):
    name: str
    kind: str  # number | integer | boolean | list | other
    default: Any
    file_value: Any | None
    override: Any | None
    has_override: bool
    effective: Any
    minimum: float | None
    maximum: float | None
    exclusive_minimum: bool
    exclusive_maximum: bool


class TuningModule(BaseModel):
    module_id: str
    description: str
    enabled: bool
    will_run: bool
    params: list[TuningParam]
    pending: bool  # saved, but the running engine still holds the old value


class TuningIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overrides: dict[str, Any]


def _kind(spec: Mapping[str, Any]) -> str:
    declared = spec.get("type")
    if declared in ("number", "integer", "boolean"):
        return str(declared)
    if declared == "array":
        return "list"
    if "anyOf" in spec:
        kinds = {s.get("type") for s in spec["anyOf"] if isinstance(s, dict)} - {"null"}
        if len(kinds) == 1:
            return _kind({"type": next(iter(kinds))})
    return "other"


def _bounds(spec: Mapping[str, Any]) -> tuple[float | None, float | None, bool, bool]:
    lo = spec.get("minimum", spec.get("exclusiveMinimum"))
    hi = spec.get("maximum", spec.get("exclusiveMaximum"))
    return (
        None if lo is None else float(lo),
        None if hi is None else float(hi),
        "exclusiveMinimum" in spec,
        "exclusiveMaximum" in spec,
    )


def bind_check(
    registry: ModuleRegistry, module_id: str, camera_id: str, params: Mapping[str, object]
) -> DetectionModule:
    """Bind a throwaway instance with `params`; raises the module's own validation error."""
    instance = registry.get(module_id).factory()
    instance.bind(ModuleContext(camera_id=camera_id, params=params, logger=get_logger("tuning")))
    return instance


def _issues(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def build_router(*, application: Application) -> APIRouter:
    router = APIRouter(prefix="/config", tags=["configuration"])
    config_dir = application.paths.config_dir
    running = {c.camera_id: c for c in application.settings.cameras}

    def registry() -> ModuleRegistry:
        assembly = application.analysis
        if assembly is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "The analysis stage is not running.")
        return assembly.chain.registry

    def camera_for(camera_id: str) -> tuple[CameraConfig, bool]:
        stored = writer.read_camera(config_dir, camera_id)
        cam = stored or running.get(camera_id)
        if cam is None:
            raise HTTPException(404, "No such camera.")
        return cam, stored is not None

    def view(cam: CameraConfig) -> list[TuningModule]:
        reg = registry()
        before = running.get(cam.camera_id)
        availability = {
            a.module_id: a
            for a in (
                application.analysis.availability(cam.camera_id) if application.analysis else ()
            )
            if hasattr(a, "module_id")
        }
        out: list[TuningModule] = []
        for descriptor in reg.descriptors():
            mid = descriptor.module_id
            file_layer = dict(load_module_params(config_dir, mid))
            overrides = dict(cam.module_params.get(mid, {}))
            effective = merge_params(descriptor.default_params, file_layer, overrides)
            try:
                instance = bind_check(reg, mid, cam.camera_id, effective)
                model = getattr(instance, "params", None)
                props: Mapping[str, Any] = (
                    model.model_json_schema().get("properties", {}) if model is not None else {}
                )
            except (ValidationError, VigilError):
                props = {}  # the stored values do not bind: shown below as plain values
            params: list[TuningParam] = []
            for name in sorted(effective):
                spec = props.get(name, {})
                lo, hi, lo_x, hi_x = _bounds(spec)
                params.append(
                    TuningParam(
                        name=name,
                        kind=_kind(spec),
                        default=descriptor.default_params.get(name),
                        file_value=file_layer.get(name),
                        override=overrides.get(name),
                        has_override=name in overrides,
                        effective=effective[name],
                        minimum=lo,
                        maximum=hi,
                        exclusive_minimum=lo_x,
                        exclusive_maximum=hi_x,
                    )
                )
            avail = availability.get(mid)
            out.append(
                TuningModule(
                    module_id=mid,
                    description=descriptor.description,
                    enabled=bool(getattr(avail, "enabled", False)),
                    will_run=bool(getattr(avail, "will_run", False)),
                    params=params,
                    pending=(before is not None and before.module_params.get(mid, {}) != overrides)
                    or (before is None),
                )
            )
        return out

    @router.get(
        "/cameras/{camera_id}/tuning", response_model=list[TuningModule], dependencies=[_VIEW]
    )
    def get_tuning(camera_id: str) -> list[TuningModule]:
        cam, _ = camera_for(camera_id)
        return view(cam)

    @router.put(
        "/cameras/{camera_id}/tuning/{module_id}",
        response_model=list[TuningModule],
        dependencies=[_CONFIGURE],
    )
    def put_tuning(
        request: Request, camera_id: str, module_id: str, body: Annotated[TuningIn, Body()]
    ) -> list[TuningModule]:
        cam, editable = camera_for(camera_id)
        if not editable:
            raise HTTPException(409, "This camera is defined outside the console; edit it by hand.")
        reg = registry()
        if not reg.has(module_id):
            raise HTTPException(404, f"No module named {module_id!r}.")
        if len(body.overrides) > MAX_OVERRIDES:
            raise HTTPException(422, "Too many overrides.")
        descriptor = reg.get(module_id).descriptor
        merged = merge_params(
            descriptor.default_params, load_module_params(config_dir, module_id), body.overrides
        )
        try:
            bind_check(reg, module_id, camera_id, merged)
        except ValidationError as exc:
            raise HTTPException(
                422, detail={"message": "Those values are not valid.", "issues": _issues(exc)}
            ) from None
        except (VigilError, ValueError) as exc:
            raise HTTPException(422, detail={"message": str(exc), "issues": []}) from None
        before = dict(cam.module_params.get(module_id, {}))
        layers = {k: dict(v) for k, v in cam.module_params.items()}
        if body.overrides:
            layers[module_id] = dict(body.overrides)
        else:
            layers.pop(module_id, None)
        updated = cam.model_copy(update={"module_params": layers})
        try:
            writer.write_camera(config_dir, writer.validate_camera(updated.model_dump()))
        except ConfigError as exc:
            raise HTTPException(
                422, detail={"message": exc.message, "issues": list(exc.issues)}
            ) from None
        audit_event(
            request,
            "camera.tuned",
            target=camera_id,
            detail={"module": module_id, "before": before, "after": dict(body.overrides)},
        )
        return view(updated)

    return router
