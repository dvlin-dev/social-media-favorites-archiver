"""Thin standard-library SQLite repository."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from social_media_favorites_archiver.processors.enrichment import EnrichmentOutcome


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

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a transaction, optionally reserving the SQLite writer immediately."""
        with self.connect() as connection:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection

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
            with self.transaction(immediate=True) as connection:
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

    def get_item(self, item_id: int) -> NormalizedItem:
        """Reconstruct one normalized item and its durable assets for workers."""
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            asset_rows = connection.execute(
                "SELECT * FROM assets WHERE item_id = ? ORDER BY ordinal, id",
                (item_id,),
            ).fetchall()
            collection_rows = connection.execute(
                """
                SELECT collections.canonical_id
                FROM item_collections
                JOIN collections ON collections.id = item_collections.collection_id
                WHERE item_collections.item_id = ? AND item_collections.state = 'active'
                ORDER BY collections.canonical_id
                """,
                (item_id,),
            ).fetchall()
        if row is None:
            raise KeyError(item_id)
        assets = [
            {
                "asset_id": asset["asset_id"],
                "ordinal": asset["ordinal"],
                "kind": asset["kind"],
                "source_url": asset["source_url"],
                "local_path": asset["local_path"],
                "sha256": asset["sha256"],
                "mime_type": asset["mime_type"],
                "size_bytes": asset["size_bytes"],
                "quality": asset["quality"],
            }
            for asset in asset_rows
        ]
        return NormalizedItem.model_validate(
            {
                "canonical_id": row["canonical_id"],
                "platform": row["platform"],
                "content_type": row["content_type"],
                "source_url": row["source_url"],
                "title": row["title"],
                "author": row["author"],
                "author_url": row["author_url"],
                "published_at": row["published_at"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "source_availability": row["source_availability"],
                "collection_canonical_ids": tuple(
                    str(collection["canonical_id"]) for collection in collection_rows
                ),
                "original_text": row["original_text"],
                "native_subtitles": json.loads(str(row["native_subtitles_json"])),
                "assets": assets,
                "content_blocks": json.loads(str(row["content_blocks_json"])),
                "source_revision": row["source_revision"],
                "metadata_fingerprint": row["metadata_fingerprint"],
                "platform_metadata": json.loads(str(row["platform_metadata_json"])),
                "adapter_version": row["adapter_version"],
            }
        )

    def active_collection_names(self, item_id: int) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT collections.name
                FROM item_collections
                JOIN collections ON collections.id = item_collections.collection_id
                WHERE item_collections.item_id = ? AND item_collections.state = 'active'
                ORDER BY collections.canonical_id
                """,
                (item_id,),
            ).fetchall()
        return tuple(str(row["name"]) for row in rows)

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

    def upsert_enrichment(
        self,
        item_id: int,
        outcome: EnrichmentOutcome,
        *,
        created_at: datetime | None = None,
    ) -> str:
        """Persist a successful structured enrichment without provider credentials."""
        payload = outcome.persistence_payload()
        provider = str(payload["provider"])
        model = str(payload["model"])
        prompt_version = str(payload["prompt_version"])
        input_hash = str(payload["input_hash"])
        identity = json.dumps(
            {
                "item_id": item_id,
                "model": model,
                "prompt_version": prompt_version,
                "input_hash": input_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        enrichment_id = f"enrichment:{hashlib.sha256(identity.encode()).hexdigest()}"
        result_json = _json(
            {"result": payload["result"], "metrics": payload["metrics"]}
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO enrichments (
                    id, item_id, provider, model, prompt_version,
                    input_hash, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id, model, prompt_version, input_hash) DO UPDATE SET
                    provider = excluded.provider,
                    result_json = excluded.result_json,
                    created_at = excluded.created_at
                """,
                (
                    enrichment_id,
                    item_id,
                    provider,
                    model,
                    prompt_version,
                    input_hash,
                    result_json,
                    _timestamp(created_at),
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM enrichments
                WHERE item_id = ? AND model = ? AND prompt_version = ? AND input_hash = ?
                """,
                (item_id, model, prompt_version, input_hash),
            ).fetchone()
        if row is None:
            raise RuntimeError("enrichment upsert did not return an identity")
        return str(row[0])

    def latest_enrichment(self, item_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT result_json FROM enrichments
                WHERE item_id = ? ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        decoded = json.loads(str(row["result_json"]))
        return decoded if isinstance(decoded, dict) else None

    def upsert_extraction(
        self,
        item_id: int,
        *,
        extraction_type: str,
        processor_version: str,
        input_fingerprint: str,
        config_hash: str,
        payload: object,
        created_at: datetime | None = None,
    ) -> str:
        """Persist a deterministic processor result and its content hash."""
        payload_json = _json(payload)
        result_hash = f"sha256:{hashlib.sha256(payload_json.encode()).hexdigest()}"
        identity = json.dumps(
            {
                "extraction_type": extraction_type,
                "processor_version": processor_version,
                "input_fingerprint": input_fingerprint,
                "config_hash": config_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        extraction_id = f"extraction:{hashlib.sha256(identity.encode()).hexdigest()}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO extractions (
                    id, item_id, extraction_type, processor_version,
                    input_fingerprint, config_hash, result_hash,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    extraction_type, input_fingerprint, processor_version, config_hash
                ) DO UPDATE SET
                    item_id = excluded.item_id,
                    result_hash = excluded.result_hash,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (
                    extraction_id,
                    item_id,
                    extraction_type,
                    processor_version,
                    input_fingerprint,
                    config_hash,
                    result_hash,
                    payload_json,
                    _timestamp(created_at),
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM extractions
                WHERE extraction_type = ? AND input_fingerprint = ?
                  AND processor_version = ? AND config_hash = ?
                """,
                (extraction_type, input_fingerprint, processor_version, config_hash),
            ).fetchone()
        if row is None:
            raise RuntimeError("extraction upsert did not return an identity")
        return str(row["id"])

    def latest_extraction(
        self,
        item_id: int,
        extraction_type: str,
    ) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM extractions
                WHERE item_id = ? AND extraction_type = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (item_id, extraction_type),
            ).fetchone()
        if row is None:
            return None
        decoded = json.loads(str(row["payload_json"]))
        return decoded if isinstance(decoded, dict) else None

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
                    last_complete_run_id = COALESCE(
                        excluded.last_complete_run_id,
                        item_collections.last_complete_run_id
                    )
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
