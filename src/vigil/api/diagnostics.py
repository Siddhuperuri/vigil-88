"""The diagnostics bundle: what a person needs to be helped with a problem, and nothing else.

**What goes in:** versions, the platform, the health the console shows, the configuration with its
secrets removed, counters, storage sizes, and the tail of the engine log.

**What never goes in, by construction rather than by care:**

  * no frame, snapshot or video, and nothing from the evidence folder (the bundle reads no file
    from it);
  * no account, password hash, session or audit row (it opens no account store);
  * no stored camera credential (the keyring is never opened) and no webhook address;
  * no absolute path: the data folder, the configuration folder and the user's profile folder are
    replaced by placeholders, in the configuration and in every log line, because a path carries
    the account name.

Redaction is applied twice, on purpose: keys that look like secrets are masked, and the text of
every string is scanned for credentials, bearer tokens and URL userinfo. The result is checked by
tests that plant secrets in all three places and fail if one survives. A log line that this
cannot make safe is still included, redacted as far as it can be; the README says that a person
should read the bundle before sending it anywhere.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import IO, Any

from vigil.api.routers.system_ops import storage_of, tail_lines
from vigil.core.redaction import redact_text, redact_value
from vigil.pipeline.app import Application
from vigil.version import __version__

SPOOL_BYTES = 8 * 1024 * 1024
LOG_LINES = 400
README = """VIGIL-88 diagnostics bundle

versions.json         the application, Python and platform, and the versions of what it uses
health.json           what the System view shows: state, cameras, workers, degradation, capabilities
config.redacted.json  the effective configuration with secrets removed
metrics.json          counters and gauges at the moment this was made
storage.json          how big the database, evidence and logs are, and the free disk space
logs/vigil.tail.jsonl the newest log records, redacted
MANIFEST.json         every file here with its SHA-256, and what is deliberately left out

Left out on purpose: frames, snapshots, video, evidence files, accounts, passwords, sessions,
the audit log, stored camera credentials, the webhook address, and absolute file paths
(replaced by placeholders).

This was redacted by software. Read it before you send it to anyone.
"""
EXCLUDED = [
    "frames, snapshots and video",
    "evidence files",
    "accounts, password hashes, sessions and the audit log",
    "stored camera credentials",
    "the webhook address",
    "absolute file paths",
]
_PROFILE_RE = re.compile(r"(?i)(?:[a-z]:)?[\\/]+(?:users|home)[\\/]+[^\\/\s\"']+")
_PACKAGES = (
    "fastapi",
    "uvicorn",
    "pydantic",
    "numpy",
    "opencv-python-headless",
    "onnxruntime",
    "onnxruntime-gpu",
    "psutil",
    "keyring",
)


class PathScrubber:
    """Replace the folders that identify a person or a machine with placeholders."""

    def __init__(self, folders: Mapping[str, Path]) -> None:
        pairs: list[tuple[str, str]] = []
        for placeholder, folder in folders.items():
            for text in {str(folder), folder.as_posix()}:
                if text and text not in {".", "/"}:
                    pairs.append((text, f"<{placeholder}>"))
        # Longest first, so a folder inside another is not half-replaced.
        self._pairs = sorted(pairs, key=lambda p: len(p[0]), reverse=True)

    def __call__(self, text: str) -> str:
        for needle, placeholder in self._pairs:
            text = re.sub(re.escape(needle), placeholder, text, flags=re.IGNORECASE)
        return _PROFILE_RE.sub("<user_profile>", text)

    def deep(self, value: object) -> object:
        if isinstance(value, str):
            return self(value)
        if isinstance(value, Mapping):
            return {str(k): self.deep(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.deep(v) for v in value]
        return value


def _safe(scrub: PathScrubber, value: object) -> object:
    return scrub.deep(redact_value(value))


def _versions() -> dict[str, Any]:
    packages: dict[str, str] = {}
    for name in _PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return {
        "vigil": __version__,
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "packages": packages,
    }


def _health(application: Application, scrub: PathScrubber) -> dict[str, Any]:
    snap = application.snapshot()
    detector = snap.detector
    body = {
        "state": snap.state.value,
        "level": snap.health.level.value,
        "issues": list(snap.health.issues),
        "uptime_ms": snap.uptime_ms,
        "degradation": {
            "step": snap.degradation.step,
            "label": snap.degradation.label,
            "reason": snap.degradation.reason,
            "suspended_cameras": list(snap.degradation.suspended),
        },
        "detector": None
        if detector is None
        else {
            "name": detector.name,
            "version": detector.version,
            "backend": detector.backend,
            "device": detector.device,
            "precision": detector.precision,
        },
        "cameras": [
            {
                "camera_id": c.camera_id,
                "state": c.state.value,
                "detail": c.detail,
                "stream_epoch": c.stream_epoch,
                "frames_received": c.frames_received,
                "frames_skipped": c.frames_skipped,
                "frames_dropped": c.frames_dropped,
            }
            for c in snap.cameras
        ],
        "workers": [
            {
                "name": w.name,
                "alive": w.alive,
                "crashed": w.crashed,
                "error": w.error,
                "heartbeat_age_ms": w.heartbeat_age_ms,
            }
            for w in snap.workers
        ],
        "capabilities": [
            {"capability": c.capability.value, "available": c.available, "provider": c.provider}
            for c in snap.capabilities
        ],
    }
    return _safe(scrub, body)  # type: ignore[return-value]


def _config(application: Application, scrub: PathScrubber) -> dict[str, Any]:
    dumped = application.settings.model_dump(mode="json")
    alerts = dumped.get("alerts")
    if isinstance(alerts, dict):
        # An address can carry a token in its path or query; only whether one is set is useful.
        alerts["webhook_url"] = "<set>" if alerts.get("webhook_url") else None
    return _safe(scrub, dumped)  # type: ignore[return-value]


def _metrics(application: Application) -> dict[str, Any]:
    snap = application.metrics.snapshot()

    def key(series: tuple[str, Any]) -> str:
        name, labels = series
        suffix = ",".join(f"{k}={v}" for k, v in labels)
        return f"{name}{{{suffix}}}" if suffix else str(name)

    return {
        "counters": {key(k): v for k, v in sorted(snap.counters.items())},
        "gauges": {key(k): v for k, v in sorted(snap.gauges.items())},
    }


def build_bundle(application: Application) -> tuple[IO[bytes], list[dict[str, Any]]]:
    """A rewound temporary file holding the zip, and its manifest."""
    paths = application.paths
    scrub = PathScrubber(
        {
            "data_dir": paths.data_dir,
            "config_dir": paths.config_dir,
            "home": Path.home(),
        }
    )
    logs = tail_lines(paths.log_file, lines=LOG_LINES, level=None)
    log_text = "".join(
        json.dumps(_safe(scrub, record), sort_keys=True, default=str) + "\n"
        for record in logs.lines
    )
    members: dict[str, str] = {
        "README.txt": README,
        "versions.json": json.dumps(_versions(), indent=2, sort_keys=True),
        "health.json": json.dumps(_health(application, scrub), indent=2, sort_keys=True),
        "config.redacted.json": json.dumps(_config(application, scrub), indent=2, sort_keys=True),
        "metrics.json": json.dumps(_metrics(application), indent=2, sort_keys=True),
        "storage.json": json.dumps(storage_of(paths).model_dump(), indent=2, sort_keys=True),
        "logs/vigil.tail.jsonl": redact_text(log_text),
    }
    manifest = [
        {
            "file": name,
            "bytes": len(text.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        for name, text in members.items()
    ]
    out = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)  # noqa: SIM115 - returned
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
        archive.writestr(
            "MANIFEST.json",
            json.dumps({"files": manifest, "left_out": EXCLUDED}, indent=2, sort_keys=True),
        )
    out.seek(0)
    return out, manifest
