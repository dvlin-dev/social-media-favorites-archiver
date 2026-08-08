from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from social_media_favorites_archiver.adapters.base import (
    AdapterError,
    AdapterErrorCode,
    CursorPage,
    FavoriteRef,
)
from social_media_favorites_archiver.models import Collection, MembershipState, Platform
from social_media_favorites_archiver.orchestrator import SyncOptions, SyncOrchestrator
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.markdown import MarkdownRenderer
from tests.integration.orchestrator_fakes import NOW, FixtureAdapter, collection


class CancellingAdapter(FixtureAdapter):
    async def list_favorites(
        self,
        selected_collection: Collection,
        cursor: str | None = None,
    ) -> CursorPage[FavoriteRef]:
        if cursor is not None:
            raise asyncio.CancelledError
        return await super().list_favorites(selected_collection, cursor)


def _orchestrator(tmp_path: Path) -> SyncOrchestrator:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    return SyncOrchestrator(database, MarkdownRenderer(tmp_path / "vault"), now=lambda: NOW)


def _membership_state(
    orchestrator: SyncOrchestrator,
    canonical_id: str,
    collection_canonical_id: str,
) -> str:
    with orchestrator.database.connect() as connection:
        return str(
            connection.execute(
                """
                SELECT item_collections.state
                FROM item_collections
                JOIN items ON items.id = item_collections.item_id
                JOIN collections ON collections.id = item_collections.collection_id
                WHERE items.canonical_id = ? AND collections.canonical_id = ?
                """,
                (canonical_id, collection_canonical_id),
            ).fetchone()[0]
        )


def test_complete_enumeration_reconciles_only_the_selected_collection(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    selected = collection("selected")
    asyncio.run(
        orchestrator.enumerate_collection(
            FixtureAdapter(("BV1a", "BV1b")),
            selected,
            options=SyncOptions(force_full_sync=True),
        )
    )
    other = Collection(
        platform=Platform.BILIBILI,
        platform_collection_id="other",
        name="Other",
    )
    other_id = orchestrator.database.upsert_collection(other)
    item_b = orchestrator.item_id("bilibili:BV1b")
    orchestrator.database.set_membership(
        item_b,
        other_id,
        MembershipState.ACTIVE,
        observed_at=NOW,
    )

    result = asyncio.run(
        orchestrator.enumerate_collection(
            FixtureAdapter(("BV1a",)),
            selected,
            options=SyncOptions(force_full_sync=True),
        )
    )

    assert result.memberships_removed == 1
    assert _membership_state(orchestrator, "bilibili:BV1b", selected.canonical_id) == "removed"
    assert orchestrator.database.derived_favorite_state(item_b) == MembershipState.ACTIVE


@pytest.mark.parametrize(
    "error_code",
    (
        AdapterErrorCode.NEEDS_AUTH,
        AdapterErrorCode.RATE_LIMITED,
        AdapterErrorCode.LAYOUT_CHANGED,
        AdapterErrorCode.ENUMERATION_INCOMPLETE,
    ),
)
def test_partial_or_failed_enumeration_never_marks_unseen_items_removed(
    tmp_path: Path,
    error_code: AdapterErrorCode,
) -> None:
    case_path = tmp_path / error_code.value
    orchestrator = _orchestrator(case_path)
    selected = collection("selected")
    asyncio.run(
        orchestrator.enumerate_collection(
            FixtureAdapter(("BV1a", "BV1b"), page_size=1),
            selected,
            options=SyncOptions(force_full_sync=True),
        )
    )
    failing = FixtureAdapter(
        ("BV1a", "BV1b"),
        page_size=1,
        fail_at_offset=1,
        error_code=error_code,
    )

    with pytest.raises(AdapterError):
        asyncio.run(
            orchestrator.enumerate_collection(
                failing,
                selected,
                options=SyncOptions(force_full_sync=True),
            )
        )

    assert _membership_state(orchestrator, "bilibili:BV1b", selected.canonical_id) == "active"
    with orchestrator.database.connect() as connection:
        run = connection.execute("SELECT status, enumeration_complete FROM runs ORDER BY rowid DESC").fetchone()
    assert tuple(run) == ("failed", 0)


def test_cancelled_enumeration_never_reconciles_unseen_memberships(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    selected = collection("selected")
    asyncio.run(
        orchestrator.enumerate_collection(
            FixtureAdapter(("BV1a", "BV1b"), page_size=1),
            selected,
            options=SyncOptions(force_full_sync=True),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            orchestrator.enumerate_collection(
                CancellingAdapter(("BV1a", "BV1b"), page_size=1),
                selected,
                options=SyncOptions(force_full_sync=True),
            )
        )

    assert _membership_state(orchestrator, "bilibili:BV1b", selected.canonical_id) == "active"
