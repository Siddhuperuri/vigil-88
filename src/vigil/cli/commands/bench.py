"""`vigil bench`: measure, never claim.

vigil bench run --detector yolox_s --device cuda --duration 120 --name gpu-yolox-s
vigil bench report            # regenerate docs/PERFORMANCE.md from stored results
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from vigil.bench.report import ReportError, save_result, write_report
from vigil.bench.runner import BenchError, run_benchmark
from vigil.bench.spec import SUSTAINED_MIN_S, BenchSpec
from vigil.bench.stats import SamplePoint
from vigil.config.loader import load_settings
from vigil.core.errors import ConfigError

bench_app = typer.Typer(
    help="Measure performance. Writes docs/PERFORMANCE.md from real runs only.",
    no_args_is_help=True,
)

ConfigDir = Annotated[Path | None, typer.Option(help="Directory holding vigil.yaml.")]
EXIT_FAILED = 1
EXIT_USAGE = 2
PROGRESS_EVERY_S = 10
_RESOLUTION = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_GIT_TIMEOUT_S = 10


def _git(*args: str, cwd: Path) -> str | None:
    exe = shutil.which("git")
    if exe is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            [exe, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _dirs(config_dir: Path | None) -> tuple[Path, Path, Path]:
    """(base dir, per-run results dir under var/, published results dir under docs/)."""
    try:
        paths = load_settings(config_dir=config_dir).paths
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE) from None
    return paths.base_dir, paths.bench_dir, paths.base_dir / "docs" / "bench-results"


@bench_app.command("run", help="Run one benchmark against the real pipeline.")
def run(
    detector: Annotated[str, typer.Option(help="Model name, e.g. yolox_s")],
    name: Annotated[str, typer.Option(help="A short label for this run")],
    device: Annotated[str, typer.Option(help="auto, cuda or cpu")] = "cuda",
    source: Annotated[str, typer.Option(help="synthetic or webcam")] = "synthetic",
    cameras: Annotated[int, typer.Option(help="Synthetic cameras")] = 1,
    resolution: Annotated[str, typer.Option(help="Source resolution, WxH")] = "640x480",
    source_fps: Annotated[float, typer.Option(help="Camera frame rate")] = 30.0,
    infer_fps: Annotated[float, typer.Option(help="Target inference rate per camera")] = 30.0,
    duration: Annotated[
        float, typer.Option(help="Seconds. 120 or more is a sustained run")
    ] = 120.0,
    window: Annotated[float, typer.Option(help="First/last comparison window, seconds")] = 30.0,
    stream: Annotated[bool, typer.Option(help="Consume the annotated stream in-process")] = True,
    publish: Annotated[bool, typer.Option(help="Copy the result into docs/bench-results/")] = False,
    config_dir: ConfigDir = None,
) -> None:
    m = _RESOLUTION.match(resolution)
    if m is None or device not in {"auto", "cuda", "cpu"} or source not in {"synthetic", "webcam"}:
        typer.echo(
            "error: use --resolution WxH, --device auto|cuda|cpu, --source synthetic|webcam",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    spec = BenchSpec(
        name=name,
        detector=detector,
        device=device,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        cameras=cameras,
        width_px=int(m.group(1)),
        height_px=int(m.group(2)),
        source_fps=source_fps,
        infer_fps=infer_fps,
        duration_s=duration,
        window_s=window,
        stream=stream,
    )
    base_dir, bench_dir, published_dir = _dirs(config_dir)
    commit = _git("rev-parse", "--short", "HEAD", cwd=base_dir)
    dirty = bool(_git("status", "--porcelain", cwd=base_dir))
    kind = "SUSTAINED" if spec.sustained else f"screening (under {SUSTAINED_MIN_S:g} s)"
    typer.echo(f"benchmark '{name}': {detector} on {device}, {kind}, {duration:g} s")

    def progress(p: SamplePoint) -> None:
        if round(p.t_s) % PROGRESS_EVERY_S != 0:
            return
        gpu = ""
        if p.gpu_util_percent is not None and p.gpu_sm_clock_mhz is not None:
            gpu = f" gpu={p.gpu_util_percent:.0f}% {p.gpu_sm_clock_mhz:.0f}MHz"
            if p.gpu_temp_c is not None:
                gpu += f" {p.gpu_temp_c:.0f}C"
        typer.echo(
            f"  t={p.t_s:5.0f}s  inferred={p.inferred / p.dt_s:6.1f}/s  skipped={p.skipped}  "
            f"dropped={p.dropped}  cpu={p.cpu_percent:.0f}%{gpu}"
        )

    try:
        result = run_benchmark(
            spec,
            config_dir=config_dir,
            on_sample=progress,
            extra_environment={"git_commit": commit or "unknown", "git_dirty": dirty},
        )
    except (BenchError, ConfigError) as exc:
        typer.echo(f"benchmark failed: {exc}", err=True)
        raise typer.Exit(EXIT_FAILED) from None

    saved = save_result(result, bench_dir)
    w = result.whole
    typer.echo(
        f"\nresult: fps mean={w.fps_mean:.1f} median={w.fps_median:.1f}  "
        f"inference p50/p95={w.infer_ms.p50:.1f}/{w.infer_ms.p95:.1f} ms  "
        f"frames inferred={w.inferred} skipped={w.skipped} dropped={w.dropped}"
        if w.fps_mean is not None
        and w.fps_median is not None
        and w.infer_ms.p50 is not None
        and w.infer_ms.p95 is not None
        else "\nresult: no frames were inferred"
    )
    typer.echo(f"saved {saved}")
    if publish:
        published_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(saved, published_dir / saved.name)
        typer.echo(f"published to {published_dir / saved.name}")


@bench_app.command("report", help="Regenerate docs/PERFORMANCE.md from docs/bench-results/.")
def report(config_dir: ConfigDir = None) -> None:
    base_dir, _, published_dir = _dirs(config_dir)
    output = base_dir / "docs" / "PERFORMANCE.md"
    try:
        n = write_report(published_dir, output)
    except ReportError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    typer.echo(f"wrote {output} from {n} stored result(s)")
