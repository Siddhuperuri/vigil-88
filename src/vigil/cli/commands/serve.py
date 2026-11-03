"""`vigil serve`: the engine as a service for the desktop shell (ADR 0016).

It differs from `vigil run --serve` in the three ways a supervising process needs:

  * **It starts with no cameras.** A first-run wizard needs a running engine before any camera
    exists, so the "no enabled cameras" refusal of `run` does not apply here.
  * **It always serves**, and `--port 0` asks the operating system for a free port, so the shell
    never has to guess one.
  * **It tells its parent what happened on stdout, one JSON object per line**, and writes logs to
    stderr only, so the two never interleave. The parent waits for `{"event": "ready"}` and reads
    the port from it.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from vigil.auth.factory import build_auth_service
from vigil.cli.commands.run import EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, _build_hub
from vigil.config.loader import load_settings
from vigil.core.errors import CapabilityError, ConfigError, VigilError
from vigil.observability.logging import configure_logging, get_logger, shutdown_logging
from vigil.pipeline.app import Application
from vigil.pipeline.health import HealthLevel
from vigil.version import __version__

if TYPE_CHECKING:
    from vigil.pipeline.streaming import StreamHub

HEALTH_INTERVAL_S = 5.0
SETUP_ENV = "VIGIL_SETUP_TOKEN"  # the supervising shell hands the engine its one-time token
_API_HINT = "install the API extra: uv sync --extra api"


def emit(event: str, **fields: object) -> None:
    """One JSON line on stdout, flushed: the supervisor's only view of the engine's lifecycle."""
    sys.stdout.write(json.dumps({"event": event, **fields}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _stop_when_stdin_closes(stop: threading.Event) -> None:
    """A daemon thread that sets `stop` when the parent closes (or loses) our standard input.

    On Windows a parent cannot deliver a polite signal to a child it did not start in a new
    process group, and `TerminateProcess` skips the shutdown that releases the camera. Closing
    the pipe is the one request that works the same on every platform, and it also fires if the
    parent dies, which is exactly when an orphaned engine must not keep the camera open.
    """

    def watch() -> None:
        try:
            while sys.stdin.read(1):
                pass  # the parent never writes; anything that arrives is ignored
        except (OSError, ValueError):
            pass  # a closed or broken pipe is the signal, not an error
        stop.set()

    threading.Thread(target=watch, name="vigil-parent-pipe", daemon=True).start()


def serve(
    config_dir: Annotated[Path | None, typer.Option(help="Directory holding vigil.yaml.")] = None,
    detector: Annotated[
        str | None, typer.Option(help="A model name such as yolox_s (see `vigil models`).")
    ] = None,
    device: Annotated[
        str | None, typer.Option(help="Compute device for the detector: auto, cuda or cpu.")
    ] = None,
    port: Annotated[
        int | None, typer.Option(help="Override api.bind_port. 0 picks a free port.")
    ] = None,
    duration: Annotated[float | None, typer.Option(help="Stop after this many seconds.")] = None,
    log_level: Annotated[str | None, typer.Option(help="Override logging.level.")] = None,
    ui_dir: Annotated[
        Path | None, typer.Option(help="Serve this built console under /app/ on the API origin.")
    ] = None,
    stop_on_stdin_eof: Annotated[
        bool,
        typer.Option(
            help="Shut down cleanly when standard input closes. The supervising shell holds the "
            "pipe, so this also covers the shell crashing, which a signal cannot on Windows."
        ),
    ] = False,
) -> None:
    overrides: dict[str, object] = {"api.accounts.enabled": True}
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
        emit("failed", stage="config", message=str(exc))
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE) from None

    configure_logging(loaded.settings.logging, log_file=loaded.paths.log_file)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if (sig := getattr(signal, name, None)) is not None:
            signal.signal(sig, request_stop)
    if stop_on_stdin_eof:
        _stop_when_stdin_closes(stop)

    restart_requested = threading.Event()
    restart_hook = None
    if stop_on_stdin_eof:
        # Only a supervised engine may restart itself: without a parent to start it again, it
        # would simply exit and stay down, which is not what "apply" means.
        def restart_hook() -> None:
            restart_requested.set()
            stop.set()

    exit_code = EXIT_OK
    hub: StreamHub | None = None
    api_server = None
    app: Application | None = None
    account_store = None
    try:
        hub = _build_hub()
        app = Application(loaded.settings, loaded.paths, stream_hub=hub)
        app.start()
        try:
            from vigil.api.app import create_app
            from vigil.api.server import ApiServer
        except ImportError as exc:
            raise CapabilityError(f"serve needs the API dependencies: {_API_HINT}") from exc
        accounts, account_store = build_auth_service(
            loaded.settings,
            loaded.paths,
            app.clock,
            setup_token=os.environ.get(SETUP_ENV) or None,
        )
        if accounts.setup_required() and accounts.setup_token_to_print:
            # Headless: nobody supplied a token, so show it once, to the person at the console.
            typer.echo(f"setup token (shown once): {accounts.setup_token_to_print}", err=True)
        api_cfg = loaded.settings.api
        api_server = ApiServer(
            create_app(app, hub, accounts=accounts, ui_dir=ui_dir, restart=restart_hook),
            host=api_cfg.bind_host,
            port=api_cfg.bind_port,
            logger=get_logger("api"),
        )
        api_server.start()
        emit(
            "ready",
            port=api_server.port,
            url=api_server.url,
            version=__version__,
            cameras=len(app.camera_health()),
            setup_required=accounts.setup_required(),
        )
        waited = 0.0
        while not stop.is_set() and (duration is None or waited < duration):
            step = (
                HEALTH_INTERVAL_S if duration is None else min(HEALTH_INTERVAL_S, duration - waited)
            )
            stop.wait(step)
            waited += step
            snap = app.snapshot()
            emit("health", level=snap.health.level.value, issues=list(snap.health.issues))
        if app.snapshot().health.level is HealthLevel.FAILED:
            exit_code = EXIT_RUNTIME
    except (VigilError, OSError) as exc:
        emit("failed", stage="startup", message=str(exc))
        typer.echo(f"startup failed: {exc}", err=True)
        exit_code = EXIT_RUNTIME
    finally:
        if hub is not None:
            hub.close()
        api_stopped = api_server.stop() if api_server is not None else True
        if account_store is not None:
            account_store.close()
        report = app.stop() if app is not None else None
        shutdown_logging()
    clean = report.clean if report is not None else True
    if restart_requested.is_set():
        emit("restart_requested")  # the supervisor relaunches at once, with no failure penalty
    emit(
        "stopped",
        clean=clean and api_stopped,
        duration_ms=None if report is None else report.duration_ms,
    )
    if not clean or not api_stopped:
        exit_code = EXIT_RUNTIME
    raise typer.Exit(exit_code)
