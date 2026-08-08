from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from social_media_favorites_archiver.config import AppSettings
from social_media_favorites_archiver.orchestrator import SyncOptions, SyncOrchestrator
from social_media_favorites_archiver.processors.enrichment import (
    EnrichmentInput,
    EnrichmentStatus,
    OpenAICompatibleEnricher,
)
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.markdown import MarkdownRenderer
from tests.integration.orchestrator_fakes import FixtureAdapter, collection, item

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _input() -> EnrichmentInput:
    return EnrichmentInput(
        title="Synthetic title",
        author="Fixture author",
        platform="xiaohongshu",
        original_text="Synthetic source body.",
        transcript="Synthetic transcript.",
        ocr_text="Synthetic OCR.",
    )


def _structured_text() -> str:
    return json.dumps(
        {
            "summary": "Synthetic summary.",
            "key_points": ["One"],
            "topics": ["Archive"],
            "tags": ["Archive"],
            "language": "en",
            "safety_notes": [],
        }
    )


def test_responses_api_success_has_strict_schema_and_sanitized_provenance() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": _structured_text()}],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
            },
        )

    outcome = OpenAICompatibleEnricher(
        enabled=True,
        environment=lambda: {
            "OPENAI_API_KEY": "fake-key-must-not-persist",
            "OPENAI_BASE_URL": "https://compatible.example.invalid/v1/",
            "OPENAI_MODEL": "fixture-model",
        },
        transport=httpx.MockTransport(handle),
    ).enrich(_input())

    assert outcome.status == EnrichmentStatus.SUCCEEDED
    assert outcome.metrics.request_characters > 0
    assert outcome.metrics.total_tokens == 20
    assert outcome.provenance is not None
    assert outcome.provenance.input_hash.startswith("sha256:")
    persisted = outcome.persistence_payload()
    serialized = json.dumps(persisted)
    assert "fake-key-must-not-persist" not in serialized
    assert "authorization" not in serialized.lower()
    request_body = json.loads(requests[0].content)
    schema = request_body["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "summary",
        "key_points",
        "topics",
        "tags",
        "language",
        "safety_notes",
    }


def test_chat_completions_is_used_when_responses_endpoint_is_unavailable() -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": {"message": "not supported"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _structured_text()}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    outcome = OpenAICompatibleEnricher(
        enabled=True,
        environment=lambda: {
            "OPENAI_API_KEY": "fake-key",
            "OPENAI_BASE_URL": "https://chat-only.example.invalid/v1",
            "OPENAI_MODEL": "fixture-model",
        },
        transport=httpx.MockTransport(handle),
    ).enrich(_input())

    assert paths == ["/v1/responses", "/v1/chat/completions"]
    assert outcome.status == EnrichmentStatus.SUCCEEDED
    assert outcome.metrics.total_tokens == 18


def test_successful_enrichment_is_persisted_idempotently_without_credentials(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    item_id = database.upsert_item(item("BV-enriched"))

    outcome = OpenAICompatibleEnricher(
        enabled=True,
        environment=lambda: {
            "OPENAI_API_KEY": "fake-key-must-not-persist",
            "OPENAI_MODEL": "fixture-model",
        },
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"output_text": _structured_text(), "usage": {"total_tokens": 9}},
            )
        ),
    ).enrich(_input())

    first_id = database.upsert_enrichment(item_id, outcome, created_at=NOW)
    second_id = database.upsert_enrichment(item_id, outcome, created_at=NOW)

    assert first_id == second_id
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM enrichments").fetchall()
    assert len(rows) == 1
    assert rows[0]["provider"] == "openai-compatible"
    assert rows[0]["model"] == "fixture-model"
    assert rows[0]["prompt_version"] == "smfa-enrichment-v1"
    assert rows[0]["input_hash"].startswith("sha256:")
    serialized = json.dumps(dict(rows[0]))
    assert "fake-key-must-not-persist" not in serialized
    assert "authorization" not in serialized.lower()


def test_archive_sync_remains_complete_with_enrichment_disabled(tmp_path: Path) -> None:
    settings = AppSettings(
        vault_path=tmp_path / "vault",
        state_db_path=tmp_path / "archive.db",
        cache_path=tmp_path / "cache",
        browser_profile_path=tmp_path / "browser",
        enrichment_enabled=False,
    )
    database = Database(settings.state_db_path)
    database.migrate()
    orchestrator = SyncOrchestrator(
        database,
        MarkdownRenderer(settings.vault_path),
        now=lambda: NOW,
    )

    sync_result = asyncio.run(
        orchestrator.enumerate_collection(
            FixtureAdapter(("BV-disabled",)),
            collection(),
            options=SyncOptions(force_full_sync=True),
        )
    )
    enrichment = OpenAICompatibleEnricher(
        enabled=settings.enrichment_enabled,
        environment=lambda: {},
    ).enrich(_input())

    assert sync_result.enumeration_complete is True
    assert sync_result.skeletons_rendered == 1
    assert enrichment.status == EnrichmentStatus.DISABLED
    assert list(settings.vault_path.rglob("*.md"))
