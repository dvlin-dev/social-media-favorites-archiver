from __future__ import annotations

import json
from collections.abc import Mapping

import httpx

from social_media_favorites_archiver.processors.enrichment import (
    EnrichmentInput,
    EnrichmentStatus,
    OpenAICompatibleEnricher,
)


def _input() -> EnrichmentInput:
    return EnrichmentInput(
        title="Local title",
        author="Fixture author",
        platform="bilibili",
        original_text="Cookie: private-session\nOriginal body",
        transcript="Spoken transcript",
        ocr_text="Visual label",
    )


def _success_payload(*, extra: bool = False) -> dict[str, object]:
    enrichment: dict[str, object] = {
        "summary": "A concise summary.",
        "key_points": ["First point"],
        "topics": ["Knowledge Management"],
        "tags": ["#Knowledge Management", "ARCHIVE"],
        "language": "en",
        "safety_notes": [],
    }
    if extra:
        enrichment["raw_response"] = "must fail validation"
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps(enrichment)}
                ],
            }
        ],
        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
    }


def test_disabled_mode_never_reads_environment_or_calls_network() -> None:
    def forbidden_environment() -> Mapping[str, str]:
        raise AssertionError("disabled enrichment must not inspect provider variables")

    def forbidden_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"disabled enrichment made a request: {request.method}")

    enricher = OpenAICompatibleEnricher(
        enabled=False,
        environment=forbidden_environment,
        transport=httpx.MockTransport(forbidden_request),
    )

    outcome = enricher.enrich(_input())

    assert outcome.status == EnrichmentStatus.DISABLED
    assert outcome.enrichment is None
    assert outcome.fallback_text == _input().archive_text
    assert "Cookie: private-session" in outcome.fallback_text


def test_missing_variables_return_local_fallback_without_network() -> None:
    def forbidden_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"missing configuration made a request: {request.method}")

    outcome = OpenAICompatibleEnricher(
        enabled=True,
        environment=lambda: {},
        transport=httpx.MockTransport(forbidden_request),
    ).enrich(_input())

    assert outcome.status == EnrichmentStatus.NOT_CONFIGURED
    assert outcome.retryable is False
    assert outcome.diagnostic_code == "enrichment.configuration_missing"
    assert outcome.fallback_text == _input().archive_text


def test_environment_is_read_at_call_time_and_request_is_text_only() -> None:
    current_environment: dict[str, str] = {}
    observed_body: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        observed_body.update(json.loads(request.content))
        assert request.url == httpx.URL("https://compatible.example.invalid/v1/responses")
        assert request.headers["authorization"] == "Bearer fake-runtime-key"
        return httpx.Response(200, json=_success_payload())

    enricher = OpenAICompatibleEnricher(
        enabled=True,
        environment=lambda: current_environment,
        transport=httpx.MockTransport(handle),
    )
    missing = enricher.enrich(_input())
    current_environment.update(
        {
            "OPENAI_API_KEY": "fake-runtime-key",
            "OPENAI_BASE_URL": "https://compatible.example.invalid/v1",
            "OPENAI_MODEL": "fixture-model",
        }
    )

    succeeded = enricher.enrich(_input())

    assert missing.status == EnrichmentStatus.NOT_CONFIGURED
    assert succeeded.status == EnrichmentStatus.SUCCEEDED
    assert succeeded.enrichment is not None
    assert succeeded.enrichment.tags == ("knowledge-management", "archive")
    assert succeeded.provenance is not None
    assert succeeded.provenance.provider == "openai-compatible"
    assert succeeded.provenance.model == "fixture-model"
    serialized_request = json.dumps(observed_body)
    assert "private-session" not in serialized_request
    assert "[REDACTED]" in serialized_request
    assert all(
        forbidden not in serialized_request
        for forbidden in ("source_url", "local_path", "audio", "video", "image", "cookie_jar")
    )
    assert observed_body["text"]["format"]["strict"] is True  # type: ignore[index]


def test_invalid_structured_output_is_retryable_and_keeps_local_text() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_payload(extra=True))

    outcome = OpenAICompatibleEnricher(
        enabled=True,
        environment=lambda: {
            "OPENAI_API_KEY": "fake-key",
            "OPENAI_MODEL": "fixture-model",
        },
        transport=httpx.MockTransport(handle),
    ).enrich(_input())

    assert outcome.status == EnrichmentStatus.RETRYABLE_FAILURE
    assert outcome.retryable is True
    assert outcome.diagnostic_code == "enrichment.schema_invalid"
    assert outcome.enrichment is None
    assert outcome.fallback_text == _input().archive_text


def test_http_errors_are_classified_without_raw_provider_content() -> None:
    def outcome_for(status_code: int):
        return OpenAICompatibleEnricher(
            enabled=True,
            environment=lambda: {
                "OPENAI_API_KEY": "fake-key",
                "OPENAI_MODEL": "fixture-model",
            },
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code,
                    json={"error": {"message": "private provider response"}},
                )
            ),
        ).enrich(_input())

    throttled = outcome_for(429)
    rejected = outcome_for(401)

    assert throttled.status == EnrichmentStatus.RETRYABLE_FAILURE
    assert throttled.diagnostic_code == "enrichment.http_429"
    assert rejected.status == EnrichmentStatus.PERMANENT_FAILURE
    assert rejected.diagnostic_code == "enrichment.http_401"
    assert "private provider response" not in throttled.model_dump_json()
    assert "private provider response" not in rejected.model_dump_json()
