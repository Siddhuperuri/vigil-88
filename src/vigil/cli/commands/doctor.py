"""`vigil doctor`: say precisely what is installed, what works and what is missing."""

from __future__ import annotations

import importlib.metadata
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vigil.cli.config_doc import render_config_reference
from vigil.config.loader import LoadedConfig, load_settings
from vigil.core.capabilities import CapabilityLedger
from vigil.core.clock import SystemClock
from vigil.core.errors import ConfigError
from vigil.vision.factory import IMPLEMENTED_BACKENDS, build_detector

MIN_PYTHON = (3, 11)
NVIDIA_SMI_TIMEOUT_S = 5


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    status: str  # ok | warn | fail | info
    detail: str


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _gpu_rows() -> list[Row]:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return [Row("GPU", "info", "nvidia-smi not found: no NVIDIA driver visible")]
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [Row("GPU", "warn", f"nvidia-smi failed: {exc}")]
    line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    if out.returncode != 0 or not line:
        return [Row("GPU", "warn", f"nvidia-smi returned {out.returncode}")]
    return [Row("GPU", "ok", line)]


def _environment_rows() -> list[Row]:
    py = sys.version_info
    rows = [
        Row(
            "Python",
            "ok" if (py.major, py.minor) >= MIN_PYTHON else "fail",
            f"{platform.python_version()} ({platform.platform()})",
        ),
    ]
    numpy_v, cv_v = _version("numpy"), _version("opencv-python-headless")
    if numpy_v and cv_v:
        rows.append(Row("capture extra", "ok", f"numpy {numpy_v}, opencv {cv_v}"))
    else:
        rows.append(
            Row(
                "capture extra",
                "warn",
                "not installed: webcam capture unavailable. Fix: uv sync --extra capture",
            )
        )
    torch_v = _version("torch")
    rows.append(
        Row(
            "torch",
            "info",
            torch_v or "not installed (not required before P1; no neural inference yet)",
        )
    )
    ort = _version("onnxruntime-gpu") or _version("onnxruntime")
    rows.append(Row("onnxruntime", "info", ort or "not installed (arrives with P1)"))
    return [*rows, *_gpu_rows()]


def _config_rows(loaded: LoadedConfig) -> list[Row]:
    s, p = loaded.settings, loaded.paths
    ok, err = p.check_writable()
    backend_ok = s.vision.backend in IMPLEMENTED_BACKENDS
    return [
        Row("config", "ok", f"layers: {', '.join(loaded.sources)}; hash {s.config_hash()[:12]}"),
        Row("data_dir", "ok" if ok else "fail", str(p.data_dir) if ok else f"not writable: {err}"),
        Row(
            "vision.backend",
            "ok" if backend_ok else "fail",
            s.vision.backend
            if backend_ok
            else f"{s.vision.backend!r} is not implemented "
            f"(implemented: {sorted(IMPLEMENTED_BACKENDS)})",
        ),
        Row(
            "cameras",
            "info",
            f"{len(s.cameras)} configured, {sum(c.enabled for c in s.cameras)} enabled",
        ),
        Row("modules", "info", "none registered (no detection modules exist yet)"),
    ]


def _probe_webcam(index: int) -> list[Row]:
    try:
        from vigil.core.errors import SourceError
        from vigil.domain.camera import SourceSpec
        from vigil.domain.enums import SourceKind
        from vigil.ingest.sources.webcam import WebcamSource
    except ImportError:
        return [Row(f"webcam {index}", "fail", "capture extra not installed")]
    src = WebcamSource(SourceSpec(SourceKind.WEBCAM, f"webcam:{index}", {"device_index": index}))
    try:
        info = src.open()
        frames = 0
        for _ in range(5):
            if src.read() is not None:
                frames += 1
    except SourceError as exc:
        return [Row(f"webcam {index}", "fail", exc.message)]
    finally:
        src.close()
    return [
        Row(
            f"webcam {index}",
            "ok",
            f"{info.width_px}x{info.height_px} via {info.backend}, "
            f"driver-reported fps {info.fps}, read {frames}/5 frames",
        )
    ]


def doctor(
    config_dir: Annotated[Path | None, typer.Option(help="Directory holding vigil.yaml.")] = None,
    probe_webcam: Annotated[
        int | None,
        typer.Option(help="Open this webcam index, read 5 frames, report. Lights the camera LED."),
    ] = None,
    dump_config: Annotated[
        Path | None, typer.Option(help="Write the generated config reference to this file.")
    ] = None,
) -> None:
    console = Console()
    rows = _environment_rows()
    loaded: LoadedConfig | None = None
    try:
        loaded = load_settings(config_dir=config_dir)
        rows += _config_rows(loaded)
    except ConfigError as exc:
        rows.append(Row("config", "fail", str(exc)))
    if probe_webcam is not None:
        rows += _probe_webcam(probe_webcam)

    style = {"ok": "green", "warn": "yellow", "fail": "red", "info": "cyan"}
    table = Table(title="VIGIL-88 doctor", show_lines=False)
    for col in ("check", "status", "detail"):
        table.add_column(col)
    for r in rows:
        table.add_row(r.name, f"[{style[r.status]}]{r.status}[/]", r.detail)
    console.print(table)

    ledger = CapabilityLedger()
    if loaded is not None and loaded.settings.vision.backend in IMPLEMENTED_BACKENDS:
        detector = build_detector(loaded.settings.vision, SystemClock())
        for capability in detector.provides:
            ledger.grant(capability, detector.descriptor.name)
        detector.close()
    caps = Table(title="capabilities (what this configuration can actually supply)")
    for col in ("capability", "available", "provider / how to provide"):
        caps.add_column(col)
    for status in ledger.table():
        caps.add_row(
            status.capability.value,
            "yes" if status.available else "no",
            status.provider if status.available else (status.how_to_provide or ""),
        )
    console.print(caps)
    console.print(
        "[dim]A capability is available only if a running component grants it. "
        "Nothing is claimed because it is planned.[/]"
    )

    if dump_config is not None:
        dump_config.parent.mkdir(parents=True, exist_ok=True)
        dump_config.write_text(render_config_reference(), encoding="utf-8")
        console.print(f"wrote {dump_config}")

    raise typer.Exit(1 if any(r.status == "fail" for r in rows) else 0)
