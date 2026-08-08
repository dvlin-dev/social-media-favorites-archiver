from __future__ import annotations

import asyncio
from pathlib import Path

from social_media_favorites_archiver.models import MembershipState
from social_media_favorites_archiver.orchestrator import (
    ProcessorVersions,
    SyncOptions,
    SyncOrchestrator,
    asset_job_key,
    derivation_job_key,
)
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.markdown import MarkdownRenderer
from tests.integration.orchestrator_fakes import NOW, FixtureAdapter, collection, fingerprint


def _orchestrator(tmp_path: Path) -> SyncOrchestrator:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    return SyncOrchestrator(database, MarkdownRenderer(tmp_path / "vault"), now=lambda: NOW)


def test_incremental_early_stop_requires_known_consecutive_stable_evidence(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    identifiers = ("BV1a", "BV1b", "BV1c", "BV1d")
    asyncio.run(
        orchestrator.enumerate_collection(
            FixtureAdapter(identifiers, page_size=1),
            collection(),
            options=SyncOptions(force_full_sync=True),
        )
    )
    incremental = FixtureAdapter(identifiers, page_size=1)

    result = asyncio.run(
        orchestrator.enumerate_collection(
            incremental,
            collection(),
            options=SyncOptions(early_stop_threshold=2),
        )
    )

    assert result.early_stopped is True
    assert result.enumeration_complete is False
    assert result.references_observed == 2
    assert incremental.fetch_calls == []

    unstable = FixtureAdapter(identifiers, page_size=1, ordering_stable=False)
    unstable_result = asyncio.run(
        orchestrator.enumerate_collection(
            unstable,
            collection(),
            options=SyncOptions(early_stop_threshold=2),
        )
    )
    assert unstable_result.early_stopped is False
    assert unstable_result.references_observed == 4

    no_total = FixtureAdapter(identifiers, page_size=1, expose_total=False)
    no_total_result = asyncio.run(
        orchestrator.enumerate_collection(
            no_total,
            collection(),
            options=SyncOptions(early_stop_threshold=2),
        )
    )
    assert no_total_result.early_stopped is False
    assert no_total_result.references_observed == 4


def test_force_full_sync_and_refavorited_known_item_are_observed_as_active(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    identifiers = ("BV1a", "BV1b", "BV1c")
    asyncio.run(
        orchestrator.enumerate_collection(
            FixtureAdapter(identifiers),
            collection(),
            options=SyncOptions(force_full_sync=True),
        )
    )
    collection_id = orchestrator.collection_id(collection().canonical_id)
    old_item_id = orchestrator.item_id("bilibili:BV1a")
    orchestrator.database.set_membership(
        old_item_id,
        collection_id,
        MembershipState.REMOVED,
        observed_at=NOW,
    )
    adapter = FixtureAdapter(identifiers, page_size=1)

    result = asyncio.run(
        orchestrator.enumerate_collection(
            adapter,
            collection(),
            options=SyncOptions(force_full_sync=True, early_stop_threshold=1),
        )
    )

    assert result.early_stopped is False
    assert result.refavorited == 1
    assert adapter.fetch_calls == ["bilibili:BV1a"]
    assert orchestrator.database.derived_favorite_state(old_item_id) == MembershipState.ACTIVE


def test_asset_and_derivation_fingerprints_are_independent() -> None:
    source = fingerprint("source")
    assets = (fingerprint("video"), fingerprint("cover"))
    first_versions = ProcessorVersions(asr="asr-v1", ocr="ocr-v1", fusion="fusion-v1")
    next_versions = first_versions.model_copy(update={"ocr": "ocr-v2"})

    assert asset_job_key("bilibili:BV1a", source) == asset_job_key(
        "bilibili:BV1a", source
    )
    assert derivation_job_key("bilibili:BV1a", assets, first_versions) != derivation_job_key(
        "bilibili:BV1a", assets, next_versions
    )
    assert asset_job_key("bilibili:BV1a", source) not in {
        derivation_job_key("bilibili:BV1a", assets, first_versions),
        derivation_job_key("bilibili:BV1a", (fingerprint("changed"),), first_versions),
    }
