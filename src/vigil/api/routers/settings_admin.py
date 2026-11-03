"""Settings the console manages: the alert policy, and moving settings between machines.

The alert policy (default threshold, per-camera and per-type rules, quiet hours) is the one part of
the main configuration the console writes, into `vigil.local.yaml`, and it **takes effect at
once**: it only changes who is interrupted, never what is recorded or detected.

Export and import are described in `api/settings_io.py`. Both need the CONFIGURE permission, are
audited, and move no secret.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ValidationError

from vigil.api.live_apply import apply_alerts
from vigil.api.session import audit_event, requires
from vigil.api.settings_io import (
    AlertPolicyDoc,
    ImportRefused,
    alert_policy_of,
    alert_section,
    apply_import,
    export_document,
    parse,
    plan_import,
)
from vigil.auth.roles import Permission
from vigil.config import settings_store
from vigil.config.schema.alerts import AlertsConfig
from vigil.core.errors import ConfigError
from vigil.pipeline.app import Application
from vigil.version import __version__

_VIEW = requires(Permission.VIEW)
_CONFIGURE = requires(Permission.CONFIGURE)
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class AlertSettingsOut(BaseModel):
    policy: AlertPolicyDoc
    channels: list[str]  # read-only: adding a channel needs a restart and lives in vigil.yaml
    applied_live: bool


class PlannedChangeOut(BaseModel):
    kind: str
    target: str
    change: str


class ImportOut(BaseModel):
    applied: bool
    changes: list[PlannedChangeOut]
    warnings: list[str]
    backup: str | None
    restart_needed: bool


def build_router(*, application: Application) -> APIRouter:
    router = APIRouter(prefix="/settings", tags=["settings"])
    config_dir = application.paths.config_dir

    def stamp() -> str:
        return application.clock.wall_utc().strftime(STAMP_FORMAT)

    def current_alerts() -> AlertsConfig:
        return application.settings.alerts

    def alerts_out(*, live: bool) -> AlertSettingsOut:
        alerts = current_alerts()
        return AlertSettingsOut(
            policy=alert_policy_of(alerts), channels=list(alerts.channels), applied_live=live
        )

    def refuse(exc: ImportRefused) -> HTTPException:
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": exc.message, "issues": exc.issues},
        )

    # ------------------------------------------------------------ alert policy

    @router.get("/alerts", response_model=AlertSettingsOut, dependencies=[_VIEW])
    def get_alerts() -> AlertSettingsOut:
        return alerts_out(live=True)

    @router.put("/alerts", response_model=AlertSettingsOut, dependencies=[_CONFIGURE])
    def put_alerts(request: Request, policy: AlertPolicyDoc) -> AlertSettingsOut:
        before = alert_policy_of(current_alerts())
        try:
            merged = AlertsConfig.model_validate(
                {**current_alerts().model_dump(mode="json"), **policy.model_dump(mode="json")}
            )
        except ValidationError as exc:
            issues = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"message": "Those alert settings are not valid.", "issues": issues},
            ) from None
        try:
            settings_store.write_local_section(
                config_dir, "alerts", alert_section(merged, config_dir), stamp=stamp()
            )
        except (ConfigError, OSError) as exc:
            raise HTTPException(500, f"The settings could not be saved: {exc}") from None
        live = apply_alerts(application, merged)
        audit_event(
            request,
            "settings.alerts_changed",
            detail={
                "before": before.model_dump(mode="json"),
                "after": policy.model_dump(mode="json"),
                "applied_live": live,
            },
        )
        return alerts_out(live=live)

    # ------------------------------------------------------------ export and import

    @router.get("/export", dependencies=[_CONFIGURE])
    def export_settings(request: Request) -> Response:
        registry = application.analysis.chain.registry if application.analysis else None
        if registry is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "The engine is not ready.")
        document = export_document(
            config_dir=config_dir,
            alerts=current_alerts(),
            inline_cameras=list(application.settings.cameras),
            registry=registry,
            app_version=__version__,
            exported_utc=application.clock.wall_utc().isoformat(),
        )
        import json

        audit_event(
            request,
            "settings.exported",
            detail={"cameras": len(document["cameras"]), "zones": len(document["zones"])},
        )
        return Response(
            content=json.dumps(document, indent=2, sort_keys=True),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="vigil88-settings.json"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/import", response_model=ImportOut, dependencies=[_CONFIGURE])
    async def import_settings(
        request: Request, dry_run: Annotated[bool, Query()] = True
    ) -> ImportOut:
        """Check a settings file (the default) or apply it (`dry_run=false`). Never deletes."""
        from vigil.api.settings_io import MAX_IMPORT_BYTES

        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_IMPORT_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The file is too large.")
        raw: Any = await request.body()
        registry = application.analysis.chain.registry if application.analysis else None
        if registry is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "The engine is not ready.")
        try:
            doc = parse(bytes(raw))
            plan = plan_import(
                doc, config_dir=config_dir, current_alerts=current_alerts(), registry=registry
            )
        except ImportRefused as exc:
            raise refuse(exc) from None
        except ConfigError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"message": exc.message, "issues": list(exc.issues)},
            ) from None
        changes = [
            PlannedChangeOut(kind=c.kind, target=c.target, change=c.change) for c in plan.changes
        ]
        if dry_run:
            audit_event(request, "settings.import_checked", detail={"changes": len(plan.effective)})
            return ImportOut(
                applied=False,
                changes=changes,
                warnings=plan.warnings,
                backup=None,
                restart_needed=any(c.kind != "alerts" for c in plan.effective),
            )
        try:
            result = apply_import(
                doc, plan, config_dir=config_dir, current_alerts=current_alerts(), stamp=stamp()
            )
        except (ConfigError, OSError) as exc:
            audit_event(request, "settings.import_failed", detail={"error": str(exc)[:200]})
            raise HTTPException(
                500, f"The import failed and the previous settings were restored: {exc}"
            ) from None
        if result.alerts is not None:
            apply_alerts(application, result.alerts)
        audit_event(
            request,
            "settings.imported",
            detail={
                "changes": [f"{c.kind}:{c.target}:{c.change}" for c in plan.effective],
                "backup": result.backup,
            },
        )
        return ImportOut(
            applied=True,
            changes=changes,
            warnings=plan.warnings,
            backup=result.backup,
            restart_needed=result.restart_needed,
        )

    return router
