"""Command-line interface for Social Media Favorites Archiver."""

from typing import NoReturn

import typer

app = typer.Typer(
    help="Archive your own social-media favorites into local Markdown.",
    no_args_is_help=True,
)


def _not_implemented(command: str) -> NoReturn:
    typer.echo(f"{command} is not implemented yet.", err=True)
    raise typer.Exit(code=2)


@app.command()
def doctor() -> None:
    """Check local prerequisites without exposing secrets."""
    _not_implemented("doctor")


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

