"""`vigil` entry point."""

from __future__ import annotations

import typer

from vigil.cli.commands.doctor import doctor
from vigil.cli.commands.run import run
from vigil.version import __version__

app = typer.Typer(
    name="vigil",
    help="VIGIL-88: real-time computer-vision incident detection and situational awareness.",
    no_args_is_help=True,
    add_completion=False,
)
app.command("run", help="Run the headless pipeline.")(run)
app.command("doctor", help="Diagnose the environment, configuration and capabilities.")(doctor)


@app.command("version", help="Print the version.")
def version() -> None:
    typer.echo(__version__)
