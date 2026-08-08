import json
from pathlib import Path

from typer.testing import CliRunner

from social_media_favorites_archiver.cli import app
from social_media_favorites_archiver.config import AppSettings
from social_media_favorites_archiver.diagnostics import run_doctor

runner = CliRunner()


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings.model_validate(
        {
            "vault_path": tmp_path / "vault",
            "state_db_path": tmp_path / "vault" / ".smfa" / "archive.db",
            "cache_path": tmp_path / "cache",
            "browser_profile_path": tmp_path / "browser-profile",
            "cache_quota_bytes": 1024 * 1024,
        }
    )


def test_structured_doctor_diagnostics_never_contain_secret_values(tmp_path: Path) -> None:
    secrets = {
        "OPENAI_API_KEY": "sk-proj-FAKE-DO-NOT-LEAK",
        "OPENAI_BASE_URL": "https://example.invalid/v1?signature=FAKE-SIGNATURE",
        "OPENAI_MODEL": "private-model-name",
    }

    report = run_doctor(_settings(tmp_path), environ=secrets)
    serialized = report.model_dump_json()

    assert report.enrichment_presence == {
        "OPENAI_API_KEY": True,
        "OPENAI_BASE_URL": True,
        "OPENAI_MODEL": True,
    }
    for secret in secrets.values():
        assert secret not in serialized


def test_doctor_covers_required_local_checks(tmp_path: Path) -> None:
    report = run_doctor(_settings(tmp_path), environ={})

    assert {check.code for check in report.checks} == {
        "python",
        "ffmpeg",
        "browser_cdp",
        "yt_dlp",
        "directories",
        "database_schema",
        "asr_backend",
        "ocr_backend",
        "disk_quota",
        "enrichment",
    }


def test_doctor_json_terminal_output_never_echoes_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "\n".join(
            (
                f"vault_path: {tmp_path / 'vault'}",
                f"cache_path: {tmp_path / 'cache'}",
                f"browser_profile_path: {tmp_path / 'browser-profile'}",
                "cache_quota_bytes: 1048576",
            )
        ),
        encoding="utf-8",
    )
    secrets = {
        "OPENAI_API_KEY": "sk-proj-FAKE-CLI-SECRET",
        "OPENAI_BASE_URL": "https://example.invalid/private-endpoint",
        "OPENAI_MODEL": "private-cli-model",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    result = runner.invoke(app, ["doctor", "--json", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["enrichment_presence"]["OPENAI_API_KEY"] is True
    for secret in secrets.values():
        assert secret not in result.output
