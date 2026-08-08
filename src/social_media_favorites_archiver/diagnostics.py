"""Safe local prerequisite diagnostics."""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from social_media_favorites_archiver.config import (
    AppSettings,
    ASRBackend,
    OCRBackend,
    select_asr_backend,
)

DiagnosticStatus = Literal["pass", "warning", "fail"]


class DiagnosticCheck(BaseModel):
    """A redacted prerequisite check."""

    code: str
    status: DiagnosticStatus
    summary: str


class DoctorReport(BaseModel):
    """Structured diagnostics containing presence booleans, never secret values."""

    status: DiagnosticStatus
    checks: list[DiagnosticCheck]
    enrichment_presence: dict[str, bool]


def _check_python() -> DiagnosticCheck:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    status: DiagnosticStatus = "pass" if sys.version_info >= (3, 11) else "fail"
    return DiagnosticCheck(code="python", status=status, summary=f"Python {version}")


def _check_executable(code: str, executable: str) -> DiagnosticCheck:
    available = shutil.which(executable) is not None
    return DiagnosticCheck(
        code=code,
        status="pass" if available else "fail",
        summary="available" if available else "not available on PATH",
    )


def _check_browser_cdp(settings: AppSettings) -> DiagnosticCheck:
    parsed = urlsplit(settings.browser_cdp_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname or "", port), timeout=0.2):
            pass
    except OSError:
        return DiagnosticCheck(
            code="browser_cdp",
            status="warning",
            summary="configured Chrome CDP endpoint is not currently reachable",
        )
    return DiagnosticCheck(
        code="browser_cdp",
        status="pass",
        summary="configured Chrome CDP endpoint is reachable",
    )


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _check_directories(settings: AppSettings) -> DiagnosticCheck:
    targets = (
        settings.vault_path,
        settings.state_db_path.parent,
        settings.cache_path,
        settings.browser_profile_path,
    )
    writable = all(os.access(_nearest_existing_parent(path), os.W_OK) for path in targets)
    return DiagnosticCheck(
        code="directories",
        status="pass" if writable else "fail",
        summary="configured directory roots are writable" if writable else "a directory root is not writable",
    )


def _check_database_schema(settings: AppSettings) -> DiagnosticCheck:
    database_path = settings.state_db_path
    if not database_path.exists():
        return DiagnosticCheck(
            code="database_schema",
            status="pass",
            summary="state database is not initialized yet",
        )
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return DiagnosticCheck(
            code="database_schema",
            status="warning",
            summary="state database exists but has no recognized schema yet",
        )
    version = 0 if row is None or row[0] is None else int(row[0])
    return DiagnosticCheck(
        code="database_schema",
        status="pass" if version == 0 else "warning",
        summary="schema is compatible" if version == 0 else "schema requires a newer application",
    )


def _available_asr_backends() -> set[ASRBackend]:
    available: set[ASRBackend] = set()
    if importlib.util.find_spec("funasr") is not None:
        available.add(ASRBackend.FUNASR)
    if importlib.util.find_spec("mlx_whisper") is not None:
        available.add(ASRBackend.MLX_WHISPER)
    if shutil.which("whisper-cli") is not None:
        available.add(ASRBackend.WHISPER_CPP)
    if importlib.util.find_spec("faster_whisper") is not None:
        available.add(ASRBackend.FASTER_WHISPER)
    return available


def _check_asr_backend(settings: AppSettings) -> DiagnosticCheck:
    selected = select_asr_backend(settings, available=_available_asr_backends())
    available = selected != ASRBackend.UNAVAILABLE
    return DiagnosticCheck(
        code="asr_backend",
        status="pass" if available else "warning",
        summary=f"selected local backend: {selected.value}",
    )


def _check_ocr_backend(settings: AppSettings) -> DiagnosticCheck:
    if settings.ocr_backend == OCRBackend.DISABLED:
        return DiagnosticCheck(code="ocr_backend", status="warning", summary="OCR is disabled")
    available = importlib.util.find_spec("rapidocr_onnxruntime") is not None
    return DiagnosticCheck(
        code="ocr_backend",
        status="pass" if available else "warning",
        summary="RapidOCR is available" if available else "RapidOCR optional dependency is not installed",
    )


def _check_disk_quota(settings: AppSettings) -> DiagnosticCheck:
    existing_root = _nearest_existing_parent(settings.cache_path)
    free = shutil.disk_usage(existing_root).free
    enough = free >= settings.cache_quota_bytes
    return DiagnosticCheck(
        code="disk_quota",
        status="pass" if enough else "fail",
        summary="free space covers the configured cache quota"
        if enough
        else "free space is below the configured cache quota",
    )


def _enrichment_presence(environ: Mapping[str, str]) -> dict[str, bool]:
    return {
        name: bool(environ.get(name))
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
    }


def _check_enrichment(settings: AppSettings, presence: Mapping[str, bool]) -> DiagnosticCheck:
    if not settings.enrichment_enabled:
        return DiagnosticCheck(
            code="enrichment",
            status="pass",
            summary="optional text-only enrichment is disabled",
        )
    ready = presence["OPENAI_API_KEY"] and presence["OPENAI_MODEL"]
    return DiagnosticCheck(
        code="enrichment",
        status="pass" if ready else "fail",
        summary="optional text-only enrichment is configured"
        if ready
        else "optional enrichment is enabled but required variables are absent",
    )


def run_doctor(
    settings: AppSettings,
    *,
    environ: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Run non-destructive checks and return a report safe for logs and JSON."""
    current_environment = os.environ if environ is None else environ
    presence = _enrichment_presence(current_environment)
    checks = [
        _check_python(),
        _check_executable("ffmpeg", "ffmpeg"),
        _check_browser_cdp(settings),
        _check_executable("yt_dlp", "yt-dlp"),
        _check_directories(settings),
        _check_database_schema(settings),
        _check_asr_backend(settings),
        _check_ocr_backend(settings),
        _check_disk_quota(settings),
        _check_enrichment(settings, presence),
    ]
    status: DiagnosticStatus = "pass"
    if any(check.status == "fail" for check in checks):
        status = "fail"
    elif any(check.status == "warning" for check in checks):
        status = "warning"
    return DoctorReport(status=status, checks=checks, enrichment_presence=presence)

