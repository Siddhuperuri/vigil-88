"""`vigil run --selftest`: start the real application on the deterministic test pattern,
push every frame through the real detector path, and check what must be true.

No hardware is touched. Exit code 0 only if every check passes.
"""

from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from vigil.config.loader import load_settings
from vigil.core.capabilities import Capability
from vigil.core.errors import ConfigError, VigilError
from vigil.observability.logging import configure_logging, shutdown_logging
from vigil.pipeline.app import Application
from vigil.pipeline.snapshot import AppState
from vigil.pipeline.workers import THREAD_PREFIX

SELFTEST_FRAMES = 60
DRAIN_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_selftest(config_dir: Path | None) -> tuple[bool, list[Check]]:
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, ok, detail))

    with tempfile.TemporaryDirectory(prefix="vigil-selftest-") as tmp:
        try:
            loaded = load_settings(
                config_dir=config_dir,
                overrides={
                    "paths.data_dir": tmp,
                    "logging.level": "WARNING",
                    "logging.file_enabled": False,
                    "vision.backend": "null",
                    "cameras": [
                        {
                            "camera_id": "selftest",
                            "source": {"kind": "synthetic", "frame_count": SELFTEST_FRAMES},
                        }
                    ],
                },
            )
        except ConfigError as exc:
            check("configuration loads", False, str(exc))
            return False, checks
        check("configuration loads", True, f"layers: {', '.join(loaded.sources)}")

        configure_logging(loaded.settings.logging, log_file=None)
        app = Application(loaded.settings, loaded.paths)
        try:
            try:
                app.start()
            except (VigilError, OSError) as exc:
                check("application starts", False, str(exc))
                return False, checks
            check("application starts", app.state is AppState.RUNNING, app.state.value)

            drained = app.wait_until_drained(DRAIN_TIMEOUT_S)
            check("all frames flow through the detector", drained)
            snap = app.snapshot()
        finally:
            report = app.stop()
            shutdown_logging()

    cam = snap.cameras[0]
    check(
        "frames received",
        cam.frames_received == SELFTEST_FRAMES,
        f"{cam.frames_received}/{SELFTEST_FRAMES}",
    )
    check(
        "frames inferred",
        snap.frames_inferred == SELFTEST_FRAMES,
        f"{snap.frames_inferred}/{SELFTEST_FRAMES}",
    )
    check(
        "no frames skipped or dropped",
        cam.frames_skipped == 0 and cam.frames_dropped == 0,
        f"skipped={cam.frames_skipped} dropped={cam.frames_dropped}",
    )
    check(
        "null detector reports no detections",
        snap.detections_total == 0,
        f"{snap.detections_total} detections",
    )
    check(
        "camera reached end of stream",
        cam.state.value == "offline",
        f"{cam.state.value}: {cam.detail}",
    )
    check("no worker crashed", not any(w.crashed for w in snap.workers))
    check(
        "no detection capability is claimed",
        not any(s.available for s in snap.capabilities if s.capability is Capability.DETECTION),
        "DETECTION unavailable, as it must be with the null detector",
    )
    check(
        "shutdown is clean",
        report.clean,
        f"{report.duration_ms:.0f} ms; stragglers={list(report.stragglers)}",
    )
    leftover = [t.name for t in threading.enumerate() if t.name.startswith(THREAD_PREFIX)]
    check("no worker threads remain", not leftover, str(leftover))
    return all(c.ok for c in checks), checks
