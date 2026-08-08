from pathlib import Path

import pytest
from pydantic import ValidationError

from social_media_favorites_archiver.config import (
    AppSettings,
    ASRBackend,
    load_settings,
    select_asr_backend,
)


def test_config_precedence_cli_over_environment_over_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("concurrency: 2\nretries: 2\n", encoding="utf-8")

    settings = load_settings(
        config_file,
        environ={"SMFA_CONCURRENCY": "3", "SMFA_RETRIES": "3"},
        cli_overrides={"concurrency": 4},
    )

    assert settings.concurrency == 4
    assert settings.retries == 3


def test_paths_expand_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "vault_path: ~/vault\ncache_path: ~/cache/smfa\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file, environ={})

    assert settings.vault_path == tmp_path / "vault"
    assert settings.cache_path == tmp_path / "cache" / "smfa"


def test_cache_quota_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"cache_quota_bytes": 0})


def test_auto_asr_backend_prefers_chinese_then_apple_fallback() -> None:
    settings = AppSettings.model_validate({"asr_backend": ASRBackend.AUTO})

    assert (
        select_asr_backend(
            settings,
            system="Darwin",
            machine="arm64",
            available={ASRBackend.FUNASR, ASRBackend.MLX_WHISPER},
        )
        == ASRBackend.FUNASR
    )
    assert (
        select_asr_backend(
            settings,
            system="Darwin",
            machine="arm64",
            available={ASRBackend.MLX_WHISPER},
        )
        == ASRBackend.MLX_WHISPER
    )


def test_optional_enrichment_is_safe_when_openai_variables_are_missing() -> None:
    settings = load_settings(environ={})

    assert settings.enrichment_enabled is False
    assert "openai_api_key" not in type(settings).model_fields
