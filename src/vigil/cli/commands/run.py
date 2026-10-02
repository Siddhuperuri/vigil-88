"""`vigil run`."""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from vigil.cli.selftest import run_selftest
from vigil.cli.sources import SourceSpecError, parse_sources
from vigil.config.loader import load_settings
from vigil.core.errors import CapabilityError, ConfigError, VigilError
from vigil.observability.logging import configure_logging, get_logger, shutdown_logging
from vigil.pipeline.app import Application
from vigil.pipeline.health import HealthLevel

if TYPE_CHECKING:
    from vigil.pipeline.streaming import StreamHub

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
_API_HINT = "install the API extra: uv sync --extra api"


def _fmt(value: float | None, digits: int = 1, unit: str = "") -> str:
    return "n/a" if value is None else f"{value:.{digits}f}{unit}"


def _status_line(app: Application) -> str:
    snap = app.snapshot()
    parts = [f"state={snap.state.value}", f"health={snap.health.level.value}"]
    for c in snap.cameras:
        parts.append(
            f"[{c.camera_id} {c.state.value} {_fmt(c.measured_fps)}fps rx={c.frames_received} "
            f"skip={c.frames_skipped} drop={c.frames_dropped}]"
        )
    lat = app.metrics.histogram("vigil_inference_latency_ms").summary()
    parts.append(
        f"inferred={snap.frames_inferred} infer_p50={_fmt(lat.p50)}ms "
        f"pipeline={_fmt(app.metrics.gauge('vigil_pipeline_fps').value)}fps "
        f"detections={snap.detections_total}"
    )
    if snap.system is not None and snap.system.gpu_utilization_percent is not None:
        s = snap.system
        parts.append(
            f"gpu={_fmt(s.gpu_utilization_percent, 0, '%')} vram={_fmt(s.gpu_memory_used_mb, 0)}MB "
            f"{_fmt(s.gpu_temperature_c, 0, 'C')}"
        )
    return " ".join(parts)


def _build_hub() -> StreamHub:
    try:
        from vigil.pipeline.streaming import StreamHub
    except ImportError as exc:
        raise CapabilityError(
            f"streaming needs OpenCV and NumPy: install the capture extra ({exc})"
        ) from exc
    return StreamHub()


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
    detector: Annotated[
        str | None,
        typer.Option(
            help="Run a real detector: a model name such as yolox_s (see `vigil models`)."
        ),
    ] = None,
    device: Annotated[
        str | None, typer.Option(help="Compute device for the detector: auto, cuda or cpu.")
    ] = None,
    serve: Annotated[
        bool, typer.Option(help="Serve the live stream, metrics and viewer over HTTP.")
    ] = False,
    port: Annotated[int | None, typer.Option(help="Override api.bind_port.")] = None,
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
    if detector:
        overrides["vision.backend"] = "onnxruntime"
        overrides["vision.weights"] = detector
    if device:
        overrides["vision.device"] = device.lower()
    if port is not None:
        overrides["api.bind_port"] = port

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
    exit_code = EXIT_OK
    hub: StreamHub | None = None
    api_server = None
    app: Application | None = None
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if (sig := getattr(signal, name, None)) is not None:
            signal.signal(sig, request_stop)

    try:
        if serve:
            hub = _build_hub()
        app = Application(loaded.settings, loaded.paths, stream_hub=hub)
        app.start()
        if serve and hub is not None:
            try:
                from vigil.api.app import create_app
                from vigil.api.server import ApiServer
            except ImportError as exc:
                raise CapabilityError(f"--serve needs the API dependencies: {_API_HINT}") from exc
            api_cfg = loaded.settings.api
            api_server = ApiServer(
                create_app(app, hub),
                host=api_cfg.bind_host,
                port=api_cfg.bind_port,
                logger=get_logger("api"),
            )
            api_server.start()
            typer.echo(f"live viewer: {api_server.url}/   api docs: {api_server.url}/api/docs")
        typer.echo(_status_line(app))
        waited = 0.0
        while not stop.is_set() and (duration is None or waited < duration):
            step = (
                min(status_interval, duration - waited) if duration is not None else status_interval
            )
            stop.wait(step)
            waited += step
            typer.echo(_status_line(app))
        final = app.snapshot()
        if final.health.level is HealthLevel.FAILED:
            exit_code = EXIT_RUNTIME
            for issue in final.health.issues:
                typer.echo(f"issue: {issue}", err=True)
    except (VigilError, OSError) as exc:
        typer.echo(f"startup failed: {exc}", err=True)
        exit_code = EXIT_RUNTIME
    finally:
        if hub is not None:
            hub.close()  # wakes connected viewers so the server can shut down promptly
        api_stopped = api_server.stop() if api_server is not None else True
        report = app.stop() if app is not None else None
        shutdown_logging()
    if report is not None:
        typer.echo(
            f"shutdown {'clean' if report.clean else 'INCOMPLETE'} in {report.duration_ms:.0f} ms"
        )
        if not report.clean:
            exit_code = EXIT_RUNTIME
    if not api_stopped:
        typer.echo("the API server did not stop within its timeout", err=True)
        exit_code = EXIT_RUNTIME
    raise typer.Exit(exit_code)
