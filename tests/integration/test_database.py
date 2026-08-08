import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    Collection,
    ContentType,
    MembershipState,
    NormalizedItem,
    Platform,
)
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.migrations import CURRENT_SCHEMA_VERSION

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SHA_A = f"sha256:{'a' * 64}"
SHA_B = f"sha256:{'b' * 64}"


def _item() -> NormalizedItem:
    return NormalizedItem(
        canonical_id="bilibili:BV1example",
        platform=Platform.BILIBILI,
        content_type=ContentType.VIDEO,
        source_url="https://www.bilibili.com/video/BV1example",
        title="Example video",
        author="Example author",
        first_seen_at=NOW,
        last_seen_at=NOW,
        source_revision="revision-1",
        metadata_fingerprint=SHA_A,
        adapter_version="fixture-v1",
    )


def test_fresh_and_repeated_migration_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    database = Database(tmp_path / "archive.db")

    database.migrate()
    database.migrate()

    assert database.current_schema_version() == CURRENT_SCHEMA_VERSION
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "items",
            "collections",
            "item_collections",
            "assets",
            "jobs",
            "runs",
            "extractions",
            "enrichments",
            "schema_migrations",
        } <= tables
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == (
            CURRENT_SCHEMA_VERSION
        )


def test_upgrade_from_immediately_previous_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "archive.db")
    previous = CURRENT_SCHEMA_VERSION - 1

    database.migrate(target_version=previous)
    assert database.current_schema_version() == previous

    database.migrate()
    assert database.current_schema_version() == CURRENT_SCHEMA_VERSION


def test_repository_keeps_lightweight_fingerprint_separate_from_asset_hash(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    item_id = database.upsert_item(_item())
    asset = Asset(
        asset_id="asset-1",
        ordinal=0,
        kind=AssetKind.VIDEO,
        sha256=SHA_B,
    )

    database.upsert_asset(item_id, asset)

    with database.connect() as connection:
        item_row = connection.execute(
            "SELECT source_revision, metadata_fingerprint FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        asset_row = connection.execute(
            "SELECT sha256 FROM assets WHERE item_id = ?",
            (item_id,),
        ).fetchone()
    assert tuple(item_row) == ("revision-1", SHA_A)
    assert asset_row[0] == SHA_B


def test_memberships_derive_removed_only_after_all_collections_are_removed(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    item_id = database.upsert_item(_item())
    collection_a = database.upsert_collection(
        Collection(
            platform=Platform.BILIBILI,
            platform_collection_id="fav-a",
            name="A",
        )
    )
    collection_b = database.upsert_collection(
        Collection(
            platform=Platform.BILIBILI,
            platform_collection_id="fav-b",
            name="B",
        )
    )
    database.set_membership(item_id, collection_a, MembershipState.ACTIVE, observed_at=NOW)
    database.set_membership(item_id, collection_b, MembershipState.ACTIVE, observed_at=NOW)

    database.set_membership(item_id, collection_a, MembershipState.REMOVED, observed_at=NOW)
    assert database.derived_favorite_state(item_id) == MembershipState.ACTIVE

    database.set_membership(item_id, collection_b, MembershipState.REMOVED, observed_at=NOW)
    assert database.derived_favorite_state(item_id) == MembershipState.REMOVED


def test_unique_identity_and_foreign_keys_are_enforced(tmp_path: Path) -> None:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    first_id = database.upsert_item(_item())
    second_id = database.upsert_item(_item())
    assert first_id == second_id

    with database.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO item_collections "
            "(item_id, collection_id, state, first_seen_at, last_seen_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (first_id, 999_999, NOW.isoformat(), NOW.isoformat()),
        )
