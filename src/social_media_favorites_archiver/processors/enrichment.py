"""Optional text-only enrichment through an OpenAI-compatible API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from social_media_favorites_archiver.models import Platform
from social_media_favorites_archiver.safety.redaction import redact_text

PROMPT_VERSION = "smfa-enrichment-v1"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
PROVIDER_NAME = "openai-compatible"
_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429}
_UNSUPPORTED_RESPONSES_STATUS = {404, 405, 501}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnrichmentStatus(StrEnum):
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


class EnrichmentInput(_FrozenModel):
    """The complete allowlist of fields eligible for cloud enrichment."""

    title: str = Field(min_length=1, max_length=500)
    author: str | None = Field(default=None, max_length=500)
    platform: Platform
    original_text: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None

    @property
    def archive_text(self) -> str:
        """Keep the local source text available independently of enrichment."""
        sections = (
            ("Title", self.title),
            ("Author", self.author),
            ("Original", self.original_text),
            ("Transcript", self.transcript),
            ("OCR", self.ocr_text),
        )
        return "\n\n".join(
            f"{name}:\n{value}" for name, value in sections if value and value.strip()
        )

    def cloud_payload(self) -> dict[str, str]:
        """Return only allowlisted, redacted text and minimal source context."""
        values = {
            "title": self.title,
            "author": self.author,
            "platform": self.platform.value,
            "original_text": self.original_text,
            "transcript": self.transcript,
            "ocr_text": self.ocr_text,
        }
        return {
            key: redact_text(value).strip()
            for key, value in values.items()
            if isinstance(value, str) and value.strip()
        }


def _clean_list(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} entries must be non-empty")
        if cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return tuple(result)


class EnrichmentPayload(_FrozenModel):
    """Strict model output used by both Responses and Chat Completions."""

    summary: str = Field(min_length=1, max_length=2_000)
    key_points: tuple[str, ...] = Field(max_length=12)
    topics: tuple[str, ...] = Field(max_length=12)
    tags: tuple[str, ...] = Field(max_length=20)
    language: str = Field(min_length=1, max_length=50)
    safety_notes: tuple[str, ...] = Field(max_length=12)

    @field_validator("summary", "language")
    @classmethod
    def strip_scalar(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("structured text fields must be non-empty")
        return cleaned

    @field_validator("key_points", "topics", "safety_notes")
    @classmethod
    def clean_text_lists(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _clean_list(value, field_name=str(info.field_name))

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in value:
            cleaned = re.sub(r"[\s_]+", "-", tag.strip().lstrip("#")).strip("-").lower()
            if not cleaned:
                raise ValueError("tags must be non-empty")
            if cleaned not in seen:
                normalized.append(cleaned)
                seen.add(cleaned)
        return tuple(normalized)


class EnrichmentProvenance(_FrozenModel):
    provider: str = PROVIDER_NAME
    model: str = Field(min_length=1)
    prompt_version: str = PROMPT_VERSION
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EnrichmentMetrics(_FrozenModel):
    request_characters: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class EnrichmentOutcome(_FrozenModel):
    status: EnrichmentStatus
    enrichment: EnrichmentPayload | None = None
    provenance: EnrichmentProvenance | None = None
    metrics: EnrichmentMetrics = EnrichmentMetrics()
    fallback_text: str
    diagnostic_code: str | None = None

    @property
    def retryable(self) -> bool:
        return self.status == EnrichmentStatus.RETRYABLE_FAILURE

    def persistence_payload(self) -> dict[str, object]:
        """Serialize only the structured result, provenance, and sanitized metrics."""
        if self.status != EnrichmentStatus.SUCCEEDED:
            raise ValueError("only successful enrichment can be persisted")
        if self.enrichment is None or self.provenance is None:
            raise ValueError("successful enrichment is missing result or provenance")
        return {
            "provider": self.provenance.provider,
            "model": self.provenance.model,
            "prompt_version": self.provenance.prompt_version,
            "input_hash": self.provenance.input_hash,
            "result": self.enrichment.model_dump(mode="json"),
            "metrics": self.metrics.model_dump(mode="json"),
        }


def _input_hash(request_text: str) -> str:
    return f"sha256:{hashlib.sha256(request_text.encode()).hexdigest()}"


def _base_url(value: str | None) -> str | None:
    candidate = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return candidate


def _usage(body: Mapping[str, object]) -> tuple[int | None, int | None, int | None]:
    raw_usage = body.get("usage")
    if not isinstance(raw_usage, dict):
        return None, None, None

    def integer(*names: str) -> int | None:
        for name in names:
            value = raw_usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    return (
        integer("input_tokens", "prompt_tokens"),
        integer("output_tokens", "completion_tokens"),
        integer("total_tokens"),
    )


def _response_text(body: Mapping[str, object]) -> str | None:
    output_text = body.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = body.get("output")
    if isinstance(output, list):
        for message in output:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        return text
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
    return None


def _has_refusal(body: Mapping[str, object]) -> bool:
    if body.get("refusal"):
        return True
    serialized = json.dumps(body.get("output", ()), ensure_ascii=False)
    return '"type": "refusal"' in serialized


class OpenAICompatibleEnricher:
    """Call Responses first, then Chat Completions for compatible providers."""

    def __init__(
        self,
        *,
        enabled: bool,
        environment: Callable[[], Mapping[str, str]] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 60,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.enabled = enabled
        self.environment = environment or (lambda: os.environ)
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.clock = clock

    def _fallback(
        self,
        source: EnrichmentInput,
        status: EnrichmentStatus,
        diagnostic_code: str,
        *,
        provenance: EnrichmentProvenance | None = None,
        metrics: EnrichmentMetrics | None = None,
    ) -> EnrichmentOutcome:
        return EnrichmentOutcome(
            status=status,
            provenance=provenance,
            metrics=metrics or EnrichmentMetrics(),
            fallback_text=source.archive_text,
            diagnostic_code=diagnostic_code,
        )

    def enrich(self, source: EnrichmentInput) -> EnrichmentOutcome:
        if not self.enabled:
            return self._fallback(
                source,
                EnrichmentStatus.DISABLED,
                "enrichment.disabled",
            )

        environment = self.environment()
        api_key = environment.get("OPENAI_API_KEY", "").strip()
        model = environment.get("OPENAI_MODEL", "").strip()
        if not api_key or not model:
            return self._fallback(
                source,
                EnrichmentStatus.NOT_CONFIGURED,
                "enrichment.configuration_missing",
            )
        base_url = _base_url(environment.get("OPENAI_BASE_URL"))
        if base_url is None:
            return self._fallback(
                source,
                EnrichmentStatus.PERMANENT_FAILURE,
                "enrichment.base_url_invalid",
            )

        request_text = json.dumps(
            source.cloud_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        provenance = EnrichmentProvenance(model=model, input_hash=_input_hash(request_text))
        schema = EnrichmentPayload.model_json_schema()
        instruction = (
            "Summarize only the supplied extracted text. Return concise key points, "
            "normalized tags, optional topics/MOC suggestions, language, and safety notes."
        )
        responses_body = {
            "model": model,
            "input": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": request_text},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "smfa_enrichment",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        chat_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": request_text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "smfa_enrichment",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        started = self.clock()
        response: httpx.Response
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self.timeout_seconds,
                headers={"authorization": f"Bearer {api_key}"},
            ) as client:
                response = client.post(f"{base_url}/responses", json=responses_body)
                if response.status_code in _UNSUPPORTED_RESPONSES_STATUS:
                    response = client.post(f"{base_url}/chat/completions", json=chat_body)
        except (httpx.TimeoutException, httpx.TransportError):
            latency = max(0, round((self.clock() - started) * 1_000))
            return self._fallback(
                source,
                EnrichmentStatus.RETRYABLE_FAILURE,
                "enrichment.transport_error",
                provenance=provenance,
                metrics=EnrichmentMetrics(
                    request_characters=len(request_text), latency_ms=latency
                ),
            )

        latency = max(0, round((self.clock() - started) * 1_000))
        if not 200 <= response.status_code < 300:
            status = (
                EnrichmentStatus.RETRYABLE_FAILURE
                if response.status_code in _RETRYABLE_HTTP_STATUS
                or response.status_code >= 500
                else EnrichmentStatus.PERMANENT_FAILURE
            )
            return self._fallback(
                source,
                status,
                f"enrichment.http_{response.status_code}",
                provenance=provenance,
                metrics=EnrichmentMetrics(
                    request_characters=len(request_text), latency_ms=latency
                ),
            )

        try:
            decoded = response.json()
        except ValueError:
            decoded = None
        if not isinstance(decoded, dict):
            return self._fallback(
                source,
                EnrichmentStatus.RETRYABLE_FAILURE,
                "enrichment.response_invalid",
                provenance=provenance,
                metrics=EnrichmentMetrics(
                    request_characters=len(request_text), latency_ms=latency
                ),
            )
        body = cast(dict[str, object], decoded)
        input_tokens, output_tokens, total_tokens = _usage(body)
        metrics = EnrichmentMetrics(
            request_characters=len(request_text),
            latency_ms=latency,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        if _has_refusal(body):
            return self._fallback(
                source,
                EnrichmentStatus.PERMANENT_FAILURE,
                "enrichment.refused",
                provenance=provenance,
                metrics=metrics,
            )
        text = _response_text(body)
        if text is None:
            return self._fallback(
                source,
                EnrichmentStatus.RETRYABLE_FAILURE,
                "enrichment.response_missing_text",
                provenance=provenance,
                metrics=metrics,
            )
        try:
            enrichment = EnrichmentPayload.model_validate_json(text)
        except ValueError:
            return self._fallback(
                source,
                EnrichmentStatus.RETRYABLE_FAILURE,
                "enrichment.schema_invalid",
                provenance=provenance,
                metrics=metrics,
            )
        return EnrichmentOutcome(
            status=EnrichmentStatus.SUCCEEDED,
            enrichment=enrichment,
            provenance=provenance,
            metrics=metrics,
            fallback_text=source.archive_text,
        )
