"""Exporting and importing settings, treating an imported file as untrusted input.

**What a settings file is.** The alert policy, the cameras that live in their own files, the zone
and line files, and the shared module parameters. Nothing else: no password, no stored camera
credential (a camera names its credential, it never carries it), no webhook address (it may hold a
token), no account, no audit row, no frame.

**How an import is handled.**

  * It is bounded: a size cap, a count cap on cameras, and every model refuses unknown keys, so a
    key this version does not know is an error rather than something silently dropped.
  * It is validated in full before anything is written. A plan is returned first (`dry_run`); the
    plan lists what would be added or replaced and warns about what will not work on this machine.
  * It never deletes. It adds and replaces; removing a camera is its own deliberate action.
  * It makes a backup of every file it is about to replace, and if a write fails part-way it puts
    the files back, so a failed import leaves the configuration as it found it.
  * The format is versioned. A file from a newer version is refused with a message saying so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vigil.api.routers.tuning import bind_check
from vigil.config import settings_store, writer
from vigil.config.module_params import load_module_params, merge_params
from vigil.config.schema.alerts import AlertRuleConfig, AlertsConfig, QuietHoursConfig, SeverityName
from vigil.config.schema.camera import CameraConfig
from vigil.config.schema.zones import ZONE_ID_PATTERN, ZoneSetConfig
from vigil.config.zones import zone_file_path
from vigil.core.errors import ConfigError, SourceError, VigilError
from vigil.events.registry import ModuleRegistry
from vigil.ingest import credentials as creds

FORMAT = "vigil88-settings"
SCHEMA_VERSION = 1
MAX_IMPORT_BYTES = 1024 * 1024
MAX_CAMERAS = 64
MAX_MODULE_PARAM_ENTRIES = 64


class AlertPolicyDoc(BaseModel):
    """The part of the alert settings that is policy. Channels and the webhook are not included."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    min_severity: SeverityName = "MODERATE"
    rules: tuple[AlertRuleConfig, ...] = ()
    quiet_hours: tuple[QuietHoursConfig, ...] = ()


class SettingsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["vigil88-settings"]
    schema_version: int
    app_version: str | None = None
    exported_utc: str | None = None
    alerts: AlertPolicyDoc | None = None
    cameras: list[CameraConfig] = Field(default_factory=list, max_length=MAX_CAMERAS)
    zones: dict[str, ZoneSetConfig] = Field(default_factory=dict, max_length=MAX_CAMERAS)
    module_params: dict[str, dict[str, Any]] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannedChange:
    kind: Literal["alerts", "camera", "zones", "module_params"]
    target: str
    change: Literal["add", "replace", "same"]


@dataclass(slots=True)
class ImportPlan:
    changes: list[PlannedChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def effective(self) -> list[PlannedChange]:
        return [c for c in self.changes if c.change != "same"]


class ImportRefused(Exception):
    """The document is not acceptable. `issues` say why, in words a person can act on."""

    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.issues = issues or []


# ------------------------------------------------------------------ export


def alert_policy_of(alerts: AlertsConfig) -> AlertPolicyDoc:
    return AlertPolicyDoc(
        enabled=alerts.enabled,
        min_severity=alerts.min_severity,
        rules=alerts.rules,
        quiet_hours=alerts.quiet_hours,
    )


def export_document(
    *,
    config_dir: Path,
    alerts: AlertsConfig,
    inline_cameras: list[CameraConfig],
    registry: ModuleRegistry,
    app_version: str,
    exported_utc: str,
) -> dict[str, Any]:
    on_disk: dict[str, CameraConfig] = {}
    for camera_id in writer.list_camera_files(config_dir):
        try:
            cam = writer.read_camera(config_dir, camera_id)
        except ConfigError:
            continue  # an unreadable file cannot be exported faithfully; leave it out
        if cam is not None:
            on_disk[cam.camera_id] = cam
    for cam in inline_cameras:  # defined in vigil.yaml: exported so a copy is complete
        on_disk.setdefault(cam.camera_id, cam)
    zones: dict[str, ZoneSetConfig] = {}
    for ref in writer.zone_refs(config_dir):
        try:
            import yaml

            raw = yaml.safe_load(zone_file_path(config_dir, ref).read_text(encoding="utf-8"))
            zones[ref] = ZoneSetConfig.model_validate(raw or {})
        except (OSError, ValueError, ValidationError):
            continue
    shared: dict[str, Any] = {}
    for module_id in registry.ids():
        params = dict(load_module_params(config_dir, module_id))
        if params:
            shared[module_id] = params
    document = SettingsDocument(
        format=FORMAT,
        schema_version=SCHEMA_VERSION,
        app_version=app_version,
        exported_utc=exported_utc,
        alerts=alert_policy_of(alerts),
        cameras=sorted(on_disk.values(), key=lambda c: c.camera_id),
        zones=zones,
        module_params=shared,
    )
    return document.model_dump(mode="json", exclude_none=False)


# ------------------------------------------------------------------ parse and plan


def parse(raw: bytes) -> SettingsDocument:
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportRefused(f"The file is larger than {MAX_IMPORT_BYTES // 1024} KiB.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ImportRefused("The file is not valid JSON.") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ImportRefused("This is not a VIGIL-88 settings file.")
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ImportRefused("The file has no schema version.")
    if version > SCHEMA_VERSION:
        raise ImportRefused(
            f"This file was made by a newer version of VIGIL-88 (settings format {version}; "
            f"this one reads format {SCHEMA_VERSION}). Update VIGIL-88 to import it."
        )
    if version < 1:
        raise ImportRefused("The settings format version is not valid.")
    try:
        return SettingsDocument.model_validate(data)
    except ValidationError as exc:
        issues = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:30]
        raise ImportRefused("The file has problems and nothing was changed.", issues) from None


def _check_modules(doc: SettingsDocument, registry: ModuleRegistry, config_dir: Path) -> None:
    issues: list[str] = []
    for module_id, params in doc.module_params.items():
        if not ZONE_ID_PATTERN.match(module_id) or not registry.has(module_id):
            issues.append(f"module_params.{module_id}: no such module")
            continue
        if len(params) > MAX_MODULE_PARAM_ENTRIES:
            issues.append(f"module_params.{module_id}: too many entries")
            continue
        merged = merge_params(registry.get(module_id).descriptor.default_params, params)
        try:
            bind_check(registry, module_id, "import", merged)
        except (ValidationError, VigilError, ValueError) as exc:
            issues.append(f"module_params.{module_id}: {_short(exc)}")
    for cam in doc.cameras:
        for module_id, overrides in cam.module_params.items():
            if not registry.has(module_id):
                issues.append(f"cameras.{cam.camera_id}.module_params.{module_id}: no such module")
                continue
            shared = doc.module_params.get(module_id) or load_module_params(config_dir, module_id)
            merged = merge_params(
                registry.get(module_id).descriptor.default_params, shared, overrides
            )
            try:
                bind_check(registry, module_id, cam.camera_id, merged)
            except (ValidationError, VigilError, ValueError) as exc:
                issues.append(f"cameras.{cam.camera_id}.module_params.{module_id}: {_short(exc)}")
    if issues:
        raise ImportRefused("The file has problems and nothing was changed.", issues[:30])


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return str(exc)


def _same_text(path: Path, text: str) -> bool:
    return path.is_file() and path.read_text(encoding="utf-8") == text


def plan_import(
    doc: SettingsDocument,
    *,
    config_dir: Path,
    current_alerts: AlertsConfig,
    registry: ModuleRegistry,
) -> ImportPlan:
    _check_modules(doc, registry, config_dir)
    plan = ImportPlan()
    ids = [c.camera_id for c in doc.cameras]
    if len(ids) != len(set(ids)):
        raise ImportRefused("The file lists the same camera more than once.")
    if doc.alerts is not None:
        same = alert_policy_of(current_alerts) == doc.alerts
        plan.changes.append(PlannedChange("alerts", "alert policy", "same" if same else "replace"))
        _alerts_valid(doc.alerts, current_alerts)
    for cam in doc.cameras:
        existing = writer.read_camera(config_dir, cam.camera_id)
        change: Literal["add", "replace", "same"] = (
            "add" if existing is None else ("same" if existing == cam else "replace")
        )
        plan.changes.append(PlannedChange("camera", cam.camera_id, change))
        _camera_warnings(cam, plan)
    for ref, zone_set in doc.zones.items():
        path = zone_file_path(config_dir, ref)  # also validates the ref as a slug
        text = writer.dump_text(zone_set)
        change = "add" if not path.is_file() else ("same" if _same_text(path, text) else "replace")
        plan.changes.append(PlannedChange("zones", ref, change))
    for module_id, params in doc.module_params.items():
        current = dict(load_module_params(config_dir, module_id))
        plan.changes.append(
            PlannedChange(
                "module_params",
                module_id,
                "same" if current == params else ("add" if not current else "replace"),
            )
        )
    return plan


def _alerts_valid(policy: AlertPolicyDoc, current: AlertsConfig) -> AlertsConfig:
    """The policy applied over today's channels; rules may only name channels that exist here."""
    try:
        return AlertsConfig.model_validate(
            {**current.model_dump(mode="json"), **policy.model_dump(mode="json")}
        )
    except ValidationError as exc:
        issues = [f"alerts.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ImportRefused("The alert policy does not fit this installation.", issues) from None


def _camera_warnings(cam: CameraConfig, plan: ImportPlan) -> None:
    ref = cam.source.credential_ref
    if ref is not None:
        try:
            stored = creds.has_credentials(ref)
        except SourceError:
            stored = False
        if not stored:
            plan.warnings.append(
                f"Camera {cam.camera_id}: the credential '{ref}' is not stored on this computer. "
                "Passwords are never exported; enter it under Cameras."
            )
    if cam.source.kind == "webcam":
        plan.warnings.append(
            f"Camera {cam.camera_id}: a webcam is chosen by its position in this computer's "
            "device list, which may differ here. Check it with Test before applying."
        )


# ------------------------------------------------------------------ apply


@dataclass(slots=True)
class ImportResult:
    plan: ImportPlan
    backup: str | None
    alerts: AlertsConfig | None  # the policy to apply live, if the document carried one
    restart_needed: bool


def apply_import(
    doc: SettingsDocument,
    plan: ImportPlan,
    *,
    config_dir: Path,
    current_alerts: AlertsConfig,
    stamp: str,
) -> ImportResult:
    """Write what the plan says is new or different. All or nothing."""
    group = f"import-{stamp}"
    touched: list[tuple[Path, bool]] = []  # (path, existed before)
    wanted = {(c.kind, c.target) for c in plan.effective}
    new_alerts: AlertsConfig | None = None
    try:
        for cam in doc.cameras:
            if ("camera", cam.camera_id) in wanted:
                path = writer.camera_file_path(config_dir, cam.camera_id)
                _keep(config_dir, path, stamp, group, touched)
                writer.write_camera(config_dir, cam)
        for ref, zone_set in doc.zones.items():
            if ("zones", ref) in wanted:
                path = zone_file_path(config_dir, ref)
                _keep(config_dir, path, stamp, group, touched)
                writer.write_zone_set(config_dir, ref, zone_set)
        for module_id, params in doc.module_params.items():
            if ("module_params", module_id) in wanted:
                path = writer.module_params_file(config_dir, module_id)
                _keep(config_dir, path, stamp, group, touched)
                writer.write_module_params(config_dir, module_id, params)
        if doc.alerts is not None and ("alerts", "alert policy") in wanted:
            new_alerts = _alerts_valid(doc.alerts, current_alerts)
            path = settings_store.local_path(config_dir)
            _keep(config_dir, path, stamp, group, touched)
            settings_store.write_local_section(
                config_dir,
                "alerts",
                alert_section(new_alerts, config_dir),
                stamp=stamp,
                make_backup=False,
            )
    except BaseException:
        _roll_back(config_dir, group, touched)
        raise
    backup = f"{settings_store.BACKUPS_DIR}/{group}" if any(e for _, e in touched) else None
    restart = any(c.kind in ("camera", "zones", "module_params") for c in plan.effective)
    return ImportResult(plan, backup, new_alerts, restart)


def alert_section(alerts: AlertsConfig, config_dir: Path) -> dict[str, object]:
    """What goes into vigil.local.yaml for the alert policy: the policy keys only, merged over
    whatever else the local file already says about alerts."""
    existing = settings_store.read_local(config_dir).get("alerts")
    base: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    policy = alert_policy_of(alerts).model_dump(mode="json")
    return {**base, **policy}


def _keep(
    config_dir: Path, path: Path, stamp: str, group: str, touched: list[tuple[Path, bool]]
) -> None:
    existed = path.is_file()
    if existed:
        settings_store.backup(config_dir, path, stamp, group=group)
    touched.append((path, existed))


def _roll_back(config_dir: Path, group: str, touched: list[tuple[Path, bool]]) -> None:
    for path, existed in reversed(touched):
        if existed:
            saved = (
                config_dir
                / settings_store.BACKUPS_DIR
                / group
                / "__".join(path.relative_to(config_dir).parts)
            )
            if saved.is_file():
                writer.atomic_write(path, saved.read_text(encoding="utf-8"))
        else:
            path.unlink(missing_ok=True)
