"""`vigil auth`: the account commands that must work with no engine and no password.

`reset-admin` is the end of the road in plan §7.6. It needs the data directory, which is the
same thing an attacker with the disk has, so it asks for a typed phrase rather than pretending
to be a barrier it is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vigil.auth.factory import build_auth_service
from vigil.config.loader import load_settings
from vigil.core.clock import SystemClock
from vigil.core.errors import ConfigError, VigilError

PHRASE = "RESET ADMINISTRATOR"

auth_app = typer.Typer(help="Account recovery (no engine needed).", no_args_is_help=True)


@auth_app.command("reset-admin")
def reset_admin(
    config_dir: Annotated[Path | None, typer.Option(help="Directory holding vigil.yaml.")] = None,
    confirm: Annotated[
        str | None, typer.Option(help=f"Type the phrase {PHRASE!r} to proceed without a prompt.")
    ] = None,
) -> None:
    """Remove every administrator and require setup again. Incidents and evidence are untouched."""
    try:
        loaded = load_settings(config_dir=config_dir)
        service, store = build_auth_service(loaded.settings, loaded.paths, SystemClock())
    except (ConfigError, VigilError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    try:
        typer.echo(
            "This removes every administrator account, ends every session, and requires the "
            "setup screen again. Other users, incidents and evidence are kept. Stop the engine "
            "first, then start it again afterwards."
        )
        typed = confirm if confirm is not None else typer.prompt(f"Type {PHRASE} to continue")
        if typed.strip() != PHRASE:
            typer.echo("Not confirmed. Nothing was changed.", err=True)
            raise typer.Exit(1)
        token = service.reset_admin()
        typer.echo("Done. Start the engine; the setup screen will ask for the setup token.")
        typer.echo("Run `vigil serve` headless to be shown a new token, or let the app supply one.")
        del (
            token
        )  # a token made here would die with this process; the running engine issues its own
    finally:
        store.close()
