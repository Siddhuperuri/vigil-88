"""`vigil openapi`: write the API's OpenAPI document without needing a camera or a port.

The console's TypeScript types are generated from this file (08 §5 rule 6), and a gate regenerates
it and fails when the committed copy is stale, so a backend change that the console has not been
rebuilt against is caught at the boundary rather than in a view.

It starts a real `Application` on a throwaway data directory, because the route set depends on what
the application built (with storage off the incident routes are absent), and the document must be
the one a running engine would serve.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from vigil.auth.factory import build_auth_service
from vigil.cli.commands.run import EXIT_RUNTIME, _build_hub
from vigil.config.loader import load_settings
from vigil.core.errors import VigilError
from vigil.observability.logging import configure_logging, shutdown_logging
from vigil.pipeline.app import Application


def openapi(
    out: Annotated[
        Path | None, typer.Option("--out", help="Write here instead of standard output.")
    ] = None,
) -> None:
    """The OpenAPI document of `vigil serve`, sorted and stable so it diffs cleanly."""
    with tempfile.TemporaryDirectory(prefix="vigil-openapi-") as scratch:
        root = Path(scratch)
        config = root / "config"
        config.mkdir()
        loaded = load_settings(
            config_dir=config,
            overrides={
                "paths.data_dir": str(root / "var"),
                "api.accounts.enabled": True,
                "logging.level": "ERROR",
            },
        )
        configure_logging(loaded.settings.logging, log_file=None)
        app = Application(loaded.settings, loaded.paths, stream_hub=_build_hub())
        store = None
        try:
            from vigil.api.app import create_app

            app.start()
            accounts, store = build_auth_service(loaded.settings, loaded.paths, app.clock)
            schema = create_app(app, app.stream_hub, accounts=accounts).openapi()  # type: ignore[arg-type]
        except VigilError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(EXIT_RUNTIME) from None
        finally:
            if store is not None:
                store.close()
            app.stop()
            shutdown_logging()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if out is None:
        sys.stdout.write(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
