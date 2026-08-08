"""Command-line interface for Social Media Favorites Archiver."""

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from social_media_favorites_archiver.config import load_settings
from social_media_favorites_archiver.diagnostics import run_doctor

app = typer.Typer(
    help="Archive your own social-media favorites into local Markdown.",
    no_args_is_help=True,
)


def _not_implemented(command: str) -> NoReturn:
    typer.echo(f"{command} is not implemented yet.", err=True)
    raise typer.Exit(code=2)


@app.command()
def doctor(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a structured redacted report."),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Read settings from this YAML file."),
    ] = None,
) -> None:
    """Check local prerequisites without exposing secrets."""
    try:
        settings = load_settings(config)
    except (OSError, ValueError, ValidationError):
        typer.echo("Configuration is invalid or unreadable; no values were displayed.", err=True)
        raise typer.Exit(code=2) from None

    report = run_doctor(settings)
    if json_output:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        typer.echo(f"doctor: {report.status}")
        for check in report.checks:
            typer.echo(f"[{check.status}] {check.code}: {check.summary}")
        typer.echo("Optional enrichment variables (presence only):")
        for name, present in report.enrichment_presence.items():
            typer.echo(f"  {name}: {'present' if present else 'absent'}")
    if report.status == "fail":
        raise typer.Exit(code=1)


@app.command()
def login() -> None:
    """Establish an authorized platform browser session."""
    _not_implemented("login")


@app.command()
def sync() -> None:
    """Synchronize favorites into the configured local vault."""
    _not_implemented("sync")


@app.command()
def status() -> None:
    """Show local synchronization and queue status."""
    _not_implemented("status")


@app.command()
def retry() -> None:
    """Retry a failed local processing job."""
    _not_implemented("retry")


@app.command()
def cleanup() -> None:
    """Preview or remove verified item-owned temporary files."""
    _not_implemented("cleanup")


if __name__ == "__main__":
    app()
