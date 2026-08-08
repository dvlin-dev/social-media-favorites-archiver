"""Thin standard-library SQLite repository."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from social_media_favorites_archiver.models import (
    Asset,
    Collection,
    MembershipState,
    NormalizedItem,
)
from social_media_favorites_archiver.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Database:
    """SQLite repository with WAL, foreign keys, and numbered migrations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def current_schema_version(self) -> int:
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if exists is None:
                return 0
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return 0 if row is None or row[0] is None else int(row[0])

    def migrate(self, *, target_version: int | None = None) -> None:
        target = CURRENT_SCHEMA_VERSION if target_version is None else target_version
        if target < 0 or target > CURRENT_SCHEMA_VERSION:
            msg = f"unsupported schema target: {target}"
            raise ValueError(msg)

        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )

        current = self.current_schema_version()
        if target < current:
            msg = "schema downgrades are not supported"
            raise ValueError(msg)
        for version in range(current + 1, target + 1):
            statements = MIGRATIONS[version]
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _timestamp()),
                )

    def upsert_item(self, item: NormalizedItem) -> int:
        serialized = item.model_dump(mode="json")
        now = _timestamp()
        values = (
            item.canonical_id,
            item.platform.value,
            item.content_type.value,
            item.source_url,
            item.title,
            item.author,
            item.author_url,
            _timestamp(item.published_at) if item.published_at else None,
            _timestamp(item.first_seen_at),
            _timestamp(item.last_seen_at),
            item.source_availability.value,
            item.original_text,
            _json(serialized["native_subtitles"]),
            _json(serialized["content_blocks"]),
            item.source_revision,
            item.metadata_fingerprint,
            _json(serialized["platform_metadata"]),
            item.adapter_version,
            now,
            now,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO items (
                    canonical_id, platform, content_type, source_url, title, author,
                    author_url, published_at, first_seen_at, last_seen_at,
                    source_availability, original_text, native_subtitles_json,
                    content_blocks_json, source_revision, metadata_fingerprint,
                    platform_metadata_json, adapter_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    platform = excluded.platform,
                    content_type = excluded.content_type,
                    source_url = excluded.source_url,
                    title = excluded.title,
                    author = excluded.author,
                    author_url = excluded.author_url,
                    published_at = excluded.published_at,
                    first_seen_at = MIN(items.first_seen_at, excluded.first_seen_at),
                    last_seen_at = MAX(items.last_seen_at, excluded.last_seen_at),
                    source_availability = excluded.source_availability,
                    original_text = excluded.original_text,
                    native_subtitles_json = excluded.native_subtitles_json,
                    content_blocks_json = excluded.content_blocks_json,
                    source_revision = excluded.source_revision,
                    metadata_fingerprint = excluded.metadata_fingerprint,
                    platform_metadata_json = excluded.platform_metadata_json,
                    adapter_version = excluded.adapter_version,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            row = connection.execute(
                "SELECT id FROM items WHERE canonical_id = ?",
                (item.canonical_id,),
            ).fetchone()
        if row is None:
            msg = "item upsert did not return an identity"
            raise RuntimeError(msg)
        return int(row[0])

    def upsert_collection(self, collection: Collection) -> int:
        now = _timestamp()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collections (
                    canonical_id, platform, platform_collection_id, name,
                    source_url, adapter_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    name = excluded.name,
                    source_url = excluded.source_url,
                    adapter_version = excluded.adapter_version,
                    updated_at = excluded.updated_at
                """,
                (
                    collection.canonical_id,
                    collection.platform.value,
                    collection.platform_collection_id,
                    collection.name,
                    collection.source_url,
                    collection.adapter_version,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM collections WHERE canonical_id = ?",
                (collection.canonical_id,),
            ).fetchone()
        if row is None:
            msg = "collection upsert did not return an identity"
            raise RuntimeError(msg)
        return int(row[0])

    def upsert_asset(self, item_id: int, asset: Asset) -> int:
        now = _timestamp()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO assets (
                    asset_id, item_id, ordinal, kind, source_url, local_path,
                    sha256, mime_type, size_bytes, quality, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    ordinal = excluded.ordinal,
                    kind = excluded.kind,
                    source_url = excluded.source_url,
                    local_path = excluded.local_path,
                    sha256 = excluded.sha256,
                    mime_type = excluded.mime_type,
                    size_bytes = excluded.size_bytes,
                    quality = excluded.quality,
                    updated_at = excluded.updated_at
                """,
                (
                    asset.asset_id,
                    item_id,
                    asset.ordinal,
                    asset.kind.value,
                    asset.source_url,
                    str(asset.local_path) if asset.local_path else None,
                    asset.sha256,
                    asset.mime_type,
                    asset.size_bytes,
                    asset.quality,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM assets WHERE asset_id = ?",
                (asset.asset_id,),
            ).fetchone()
        if row is None:
            msg = "asset upsert did not return an identity"
            raise RuntimeError(msg)
        return int(row[0])

    def set_membership(
        self,
        item_id: int,
        collection_id: int,
        state: MembershipState,
        *,
        observed_at: datetime,
        last_complete_run_id: str | None = None,
    ) -> None:
        observed = _timestamp(observed_at)
        removed_at = observed if state == MembershipState.REMOVED else None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO item_collections (
                    item_id, collection_id, state, first_seen_at, last_seen_at,
                    removed_at, last_complete_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id, collection_id) DO UPDATE SET
                    state = excluded.state,
                    last_seen_at = excluded.last_seen_at,
                    removed_at = excluded.removed_at,
                    last_complete_run_id = excluded.last_complete_run_id
                """,
                (
                    item_id,
                    collection_id,
                    state.value,
                    observed,
                    observed,
                    removed_at,
                    last_complete_run_id,
                ),
            )

    def derived_favorite_state(self, item_id: int) -> MembershipState:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN state = 'active' THEN 1 ELSE 0 END) AS active
                FROM item_collections
                WHERE item_id = ?
                """,
                (item_id,),
            ).fetchone()
        active = 0 if row is None or row["active"] is None else int(row["active"])
        return MembershipState.ACTIVE if active > 0 else MembershipState.REMOVED
