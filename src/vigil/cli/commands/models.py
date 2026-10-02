"""`vigil models`: list, fetch, verify and register detector weights."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vigil.config.loader import load_settings
from vigil.core.clock import SystemClock
from vigil.core.errors import CapabilityError, ConfigError
from vigil.vision.models import (
    KNOWN_MODELS,
    fetch_model,
    load_manifest,
    register_model,
    verify_model,
)

models_app = typer.Typer(
    help="Manage detector weights (never downloaded implicitly).", no_args_is_help=True
)

ConfigDir = Annotated[Path | None, typer.Option(help="Directory holding vigil.yaml.")]
EXIT_FAILED = 1
EXIT_USAGE = 2


def _models_dir(config_dir: Path | None) -> Path:
    try:
        return load_settings(config_dir=config_dir).paths.models_dir
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE) from None


@models_app.command("list", help="Show known models and what is present and verified locally.")
def list_models(config_dir: ConfigDir = None) -> None:
    models_dir = _models_dir(config_dir)
    manifest = load_manifest(models_dir)
    typer.echo(f"models dir: {models_dir}")
    for spec in KNOWN_MODELS.values():
        entry = manifest.get(spec.filename)
        if entry is None:
            state = "not downloaded"
        else:
            try:
                verify_model(models_dir, spec.name)
                state = f"verified sha256={entry.sha256[:12]}..."
            except CapabilityError as exc:
                state = f"PROBLEM: {exc.message}"
        typer.echo(
            f"  {spec.name:<12} {spec.input_size_px}px  {spec.size_bytes / 1e6:5.1f} MB  "
            f"{spec.license:<11} {state}"
        )
    extras = sorted(set(manifest) - {s.filename for s in KNOWN_MODELS.values()})
    for name in extras:
        typer.echo(f"  {name:<12} (registered locally)  sha256={manifest[name].sha256[:12]}...")


@models_app.command("fetch", help="Download registry models over HTTPS and record their SHA-256.")
def fetch(
    names: Annotated[list[str], typer.Argument(help="Model names, e.g. yolox_s")],
    config_dir: ConfigDir = None,
) -> None:
    unknown = [n for n in names if n not in KNOWN_MODELS]
    if unknown:
        typer.echo(
            f"unknown model(s): {', '.join(unknown)}; known: {', '.join(KNOWN_MODELS)}", err=True
        )
        raise typer.Exit(EXIT_USAGE)
    models_dir = _models_dir(config_dir)
    failed = False
    for name in names:
        spec = KNOWN_MODELS[name]
        typer.echo(f"{name}: {spec.url} ({spec.size_bytes / 1e6:.1f} MB, {spec.license})")
        try:
            entry = fetch_model(spec, models_dir, clock=SystemClock())
        except CapabilityError as exc:
            typer.echo(f"  FAILED: {exc.message}", err=True)
            failed = True
            continue
        typer.echo(f"  ok  sha256={entry.sha256}")
    raise typer.Exit(EXIT_FAILED if failed else 0)


@models_app.command("verify", help="Check every manifest entry against the file on disk.")
def verify(config_dir: ConfigDir = None) -> None:
    models_dir = _models_dir(config_dir)
    manifest = load_manifest(models_dir)
    if not manifest:
        typer.echo("no models recorded")
        raise typer.Exit(0)
    bad = 0
    for filename in sorted(manifest):
        try:
            verify_model(models_dir, filename)
            typer.echo(f"  ok       {filename}")
        except CapabilityError as exc:
            bad += 1
            typer.echo(f"  FAILED   {filename}: {exc.message}", err=True)
    raise typer.Exit(EXIT_FAILED if bad else 0)


@models_app.command("register", help="Record a model file already placed in the models dir.")
def register(
    filename: Annotated[str, typer.Argument(help="File name inside the models dir")],
    license: Annotated[str, typer.Option(help="SPDX licence of the weights")],
    source: Annotated[str, typer.Option(help="Where the file came from")] = "registered locally",
    config_dir: ConfigDir = None,
) -> None:
    try:
        entry = register_model(
            _models_dir(config_dir), filename, license=license, source=source, clock=SystemClock()
        )
    except CapabilityError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    typer.echo(f"recorded {entry.file} sha256={entry.sha256}")
