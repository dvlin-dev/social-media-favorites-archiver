from typer.testing import CliRunner

from social_media_favorites_archiver.cli import app

runner = CliRunner()


def test_help_exposes_planned_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("doctor", "login", "sync", "status", "retry", "cleanup"):
        assert command in result.output
