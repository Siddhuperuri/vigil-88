"""Applying the configuration changes that can safely take effect without a restart.

**What is live, and what is not, and why.** The engine builds each camera's capture path, module
bindings and capability set once, at start (ADR 0017). A change that alters any of those needs the
controlled restart. Two things do not:

  * **Zones and tripwires, when the camera's capability set is unchanged.** The scene analyzer
    accepts a swap on its own thread (`request_swap`). It is refused here when the edit would add
    or remove the ZONES capability, because modules are bound from it: a camera that had no zones
    and now has some needs `intrusion` bound, which only a restart does.
  * **Alert rules and quiet hours** (`settings_admin`), which only change who is interrupted.

Everything else stays pending and the console says "applies after restart". Nothing here pretends
otherwise: a skipped change is returned with its reason.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vigil.config import writer
from vigil.config.schema.alerts import AlertsConfig
from vigil.config.zones import zone_file_path
from vigil.core.capabilities import Capability
from vigil.pipeline.app import Application
from vigil.pipeline.assembly import load_camera_zones


@dataclass(frozen=True, slots=True)
class Skipped:
    target: str
    reason: str


@dataclass(frozen=True, slots=True)
class LiveOutcome:
    applied: tuple[str, ...]
    skipped: tuple[Skipped, ...]


def file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def apply_zone_edits(
    application: Application, config_dir: Path, baseline: dict[str, str | None]
) -> LiveOutcome:
    """Swap in every changed zone file that can be swapped, and update `baseline` for those."""
    assembly = application.analysis
    applied: list[str] = []
    skipped: list[Skipped] = []
    if assembly is None:
        return LiveOutcome((), (Skipped("zones", "the analysis stage is not running"),))
    done_refs: set[str] = set()
    for camera in application.settings.cameras:
        ref = camera.zones_ref
        if ref is None:
            continue
        now = file_hash(zone_file_path(config_dir, ref))
        if now == baseline.get(ref):
            continue
        loaded = load_camera_zones(
            config_dir, camera, max_zones=application.settings.scene.max_zones_per_camera
        )
        had = Capability.ZONES in assembly.capabilities_by_camera.get(camera.camera_id, frozenset())
        emptied = loaded.issue is not None and "defines no zones" in loaded.issue
        if loaded.issue is not None and not emptied:
            skipped.append(Skipped(ref, f"not applied, the file is not usable: {loaded.issue}"))
            continue
        if loaded.any != had:
            skipped.append(
                Skipped(
                    ref,
                    "this edit adds or removes the camera's zones capability, which decides "
                    "which modules run; it applies after a restart",
                )
            )
            continue
        if not assembly.update_zones(camera.camera_id, loaded.zones, loaded.lines):
            skipped.append(Skipped(ref, f"camera {camera.camera_id} is not being analysed"))
            continue
        done_refs.add(ref)
        applied.append(ref)
    for ref in done_refs:
        baseline[ref] = file_hash(zone_file_path(config_dir, ref))
    # Zone files that changed but that no running camera points at yet (a camera that has just been
    # given its first zones): they cannot be swapped into anything, and silence here would read as
    # "nothing to apply".
    reported = done_refs | {s.target for s in skipped}
    for ref in sorted(set(baseline) | set(writer.zone_refs(config_dir))):
        if ref not in reported and file_hash(zone_file_path(config_dir, ref)) != baseline.get(ref):
            skipped.append(
                Skipped(
                    ref, "no running camera uses this zone file yet; it applies after a restart"
                )
            )
    return LiveOutcome(tuple(applied), tuple(skipped))


def apply_alerts(application: Application, alerts: AlertsConfig) -> bool:
    """Make a new alert policy the one in force, now. Channels are not rebuilt (that needs a
    restart); rules, quiet hours, the threshold and the on/off switch are."""
    response = application.response
    if response is None:
        return False
    response.alerts.reconfigure(alerts)
    # The effective configuration changed, so the hash stamped on later incidents must say so.
    application.settings = application.settings.model_copy(update={"alerts": alerts})
    return True
