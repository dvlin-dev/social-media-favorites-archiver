"""Typed, local-only application configuration."""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Platform(StrEnum):
    """Supported collection platforms."""

    BILIBILI = "bilibili"
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"


class ASRBackend(StrEnum):
    """Local speech-recognition backend choices."""

    AUTO = "auto"
    FUNASR = "funasr"
    MLX_WHISPER = "mlx-whisper"
    WHISPER_CPP = "whisper-cpp"
    FASTER_WHISPER = "faster-whisper"
    UNAVAILABLE = "unavailable"


class OCRBackend(StrEnum):
    """Local OCR backend choices."""

    RAPIDOCR = "rapidocr"
    DISABLED = "disabled"


class CleanupPolicy(StrEnum):
    """Temporary-media retention policy."""

    AFTER_VERIFIED = "after-verified"
    NEVER = "never"


class AppSettings(BaseSettings):
    """Validated settings with no credential fields."""

    model_config = SettingsConfigDict(
        env_prefix="SMFA_",
        extra="forbid",
        validate_default=True,
    )

    vault_path: Path = Path("~/Documents/Social Media Favorites")
    state_db_path: Path = Path(
        "~/Documents/Social Media Favorites/.social-media-favorites-archiver/archive.db"
    )
    cache_path: Path = Path("~/.cache/social-media-favorites-archiver")
    cache_quota_bytes: int = Field(default=20 * 1024**3, ge=1)
    browser_cdp_url: str = "http://127.0.0.1:9222"
    browser_profile_path: Path = Path("~/.local/share/social-media-favorites-archiver/browser")
    enabled_platforms: tuple[Platform, ...] = tuple(Platform)
    concurrency: int = Field(default=2, ge=1, le=8)
    retries: int = Field(default=3, ge=0, le=20)
    early_stop_threshold: int = Field(default=20, ge=1)
    cleanup_policy: CleanupPolicy = CleanupPolicy.AFTER_VERIFIED
    asr_backend: ASRBackend = ASRBackend.AUTO
    asr_model: str = "funasr-paraformer-zh"
    ocr_backend: OCRBackend = OCRBackend.RAPIDOCR
    terminology_dictionary: Path | None = None
    enrichment_enabled: bool = False

    @field_validator(
        "vault_path",
        "state_db_path",
        "cache_path",
        "browser_profile_path",
        "terminology_dictionary",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: object) -> object:
        """Expand user and environment markers without requiring the path to exist."""
        if value is None:
            return None
        if isinstance(value, (str, Path)):
            return Path(os.path.expandvars(str(value))).expanduser()
        return value

    @field_validator("enabled_platforms", mode="before")
    @classmethod
    def parse_platforms(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("browser_cdp_url")
    @classmethod
    def validate_browser_cdp_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            msg = "browser_cdp_url must be an HTTP(S) URL with a host"
            raise ValueError(msg)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            msg = "browser_cdp_url must not contain credentials, query parameters, or fragments"
            raise ValueError(msg)
        return value.rstrip("/")


def _decode_environment_value(value: str) -> Any:
    """Decode ordinary scalar/list values without evaluating executable input."""
    try:
        decoded = yaml.safe_load(value)
    except yaml.YAMLError:
        return value
    return value if decoded is None else decoded


def _environment_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    allowed = set(AppSettings.model_fields)
    overrides: dict[str, Any] = {}
    for name, value in environ.items():
        if not name.startswith("SMFA_"):
            continue
        field_name = name.removeprefix("SMFA_").lower()
        if field_name in allowed:
            overrides[field_name] = _decode_environment_value(value)
    return overrides


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as config_file:
        loaded = yaml.safe_load(config_file)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        msg = "configuration root must be a mapping with string keys"
        raise ValueError(msg)
    return dict(loaded)


def load_settings(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, object] | None = None,
) -> AppSettings:
    """Load defaults, then file, environment, and CLI overrides in that order."""
    merged: dict[str, Any] = {}
    if config_path is not None:
        merged.update(_load_yaml(Path(config_path).expanduser()))
    merged.update(_environment_overrides(os.environ if environ is None else environ))
    if cli_overrides is not None:
        merged.update({key: value for key, value in cli_overrides.items() if value is not None})
    return AppSettings.model_validate(merged)


def select_asr_backend(
    settings: AppSettings,
    *,
    system: str | None = None,
    machine: str | None = None,
    available: set[ASRBackend] | None = None,
) -> ASRBackend:
    """Resolve the configured local ASR backend without selecting any cloud service."""
    if settings.asr_backend != ASRBackend.AUTO:
        return settings.asr_backend

    choices = available or set()
    if ASRBackend.FUNASR in choices:
        return ASRBackend.FUNASR

    current_system = system or platform.system()
    current_machine = machine or platform.machine()
    if current_system == "Darwin" and current_machine == "arm64":
        for backend in (ASRBackend.MLX_WHISPER, ASRBackend.WHISPER_CPP):
            if backend in choices:
                return backend
    if current_system == "Linux" and ASRBackend.FASTER_WHISPER in choices:
        return ASRBackend.FASTER_WHISPER
    for backend in (ASRBackend.WHISPER_CPP, ASRBackend.FASTER_WHISPER):
        if backend in choices:
            return backend
    return ASRBackend.UNAVAILABLE

