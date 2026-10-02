"""`vigil run`."""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from typing import Annotated

import typer

from vigil.cli.selftest import run_selftest
from vigil.cli.sources import SourceSpecError, parse_sources
from vigil.config.loader import load_settings
from vigil.core.errors import ConfigError, VigilError
from vigil.observability.logging import configure_logging, shutdown_logging
from vigil.pipeline.app import Application
from vigil.pipeline.health import HealthLevel
from vigil.pipeline.snapshot import AppSnapshot

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2


def _status_line(snap: AppSnapshot) -> str:
    parts = [f"state={snap.state.value}", f"health={snap.health.level.value}"]
    for c in snap.cameras:
        fps = f"{c.measured_fps:.1f}fps" if c.measured_fps is not None else "n/a"
        parts.append(
            f"[{c.camera_id} {c.state.value} {fps} rx={c.frames_received} "
            f"skip={c.frames_skipped} drop={c.frames_dropped}]"
        )
    parts.append(f"inferred={snap.frames_inferred} detections={snap.detections_total}")
    return " ".join(parts)


def run(
    config_dir: Annotated[Path | None, typer.Option(help="Directory holding vigil.yaml.")] = None,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help="Ad-hoc source, replaces configured cameras: webcam:0, synthetic[:N].",
        ),
    ] = None,
    duration: Annotated[float | None, typer.Option(help="Stop after this many seconds.")] = None,
    status_interval: Annotated[float, typer.Option(help="Seconds between status lines.")] = 5.0,
    selftest: Annotated[bool, typer.Option(help="Run the built-in self-test and exit.")] = False,
    log_level: Annotated[str | None, typer.Option(help="Override logging.level.")] = None,
) -> None:
    if selftest:
        ok, checks = run_selftest(config_dir)
        for c in checks:
            typer.echo(
                f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}"
                + (f"  ({c.detail})" if c.detail else "")
            )
        typer.echo("selftest PASSED" if ok else "selftest FAILED")
        raise typer.Exit(EXIT_OK if ok else EXIT_RUNTIME)

    overrides: dict[str, object] = {}
    if source:
        try:
            overrides["cameras"] = parse_sources(source)
        except SourceSpecError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(EXIT_USAGE) from None
    if log_level:
        overrides["logging.level"] = log_level.upper()

    try:
        loaded = load_settings(config_dir=config_dir, overrides=overrides)
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE) from None

    enabled = [c for c in loaded.settings.cameras if c.enabled]
    if not enabled:
        typer.echo(
            "error: no enabled cameras. Enable one in config/cameras/ or pass --source "
            "(e.g. --source webcam:0).",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    configure_logging(loaded.settings.logging, log_file=loaded.paths.log_file)
    app = Application(loaded.settings, loaded.paths)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if (sig := getattr(signal, name, None)) is not None:
            signal.signal(sig, request_stop)

    exit_code = EXIT_OK
    try:
        app.start()
        typer.echo(_status_line(app.snapshot()))
        waited = 0.0
        while not stop.is_set() and (duration is None or waited < duration):
            step = (
                min(status_interval, duration - waited) if duration is not None else status_interval
            )
            stop.wait(step)
            waited += step
            typer.echo(_status_line(app.snapshot()))
        final = app.snapshot()
        if final.health.level is HealthLevel.FAILED:
            exit_code = EXIT_RUNTIME
            for issue in final.health.issues:
                typer.echo(f"issue: {issue}", err=True)
    except (VigilError, OSError) as exc:
        typer.echo(f"startup failed: {exc}", err=True)
        exit_code = EXIT_RUNTIME
    finally:
        report = app.stop()
        shutdown_logging()
    typer.echo(
        f"shutdown {'clean' if report.clean else 'INCOMPLETE'} in {report.duration_ms:.0f} ms"
    )
    if not report.clean:
        exit_code = EXIT_RUNTIME
    raise typer.Exit(exit_code)
