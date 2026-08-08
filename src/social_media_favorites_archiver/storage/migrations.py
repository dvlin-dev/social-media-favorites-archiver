"""Numbered, transactional SQLite schema migrations."""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = 2


MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL CHECK (platform IN ('bilibili', 'xiaohongshu', 'douyin')),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            enumeration_complete INTEGER NOT NULL DEFAULT 0 CHECK (enumeration_complete IN (0, 1)),
            stats_json TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_id TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL CHECK (platform IN ('bilibili', 'xiaohongshu', 'douyin')),
            content_type TEXT NOT NULL CHECK (
                content_type IN ('video', 'article', 'image_post', 'gallery')
            ),
            source_url TEXT NOT NULL,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            author_url TEXT,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            source_availability TEXT NOT NULL CHECK (
                source_availability IN ('available', 'unavailable', 'deleted', 'restricted')
            ),
            original_text TEXT,
            native_subtitles_json TEXT NOT NULL DEFAULT '[]',
            content_blocks_json TEXT NOT NULL DEFAULT '[]',
            source_revision TEXT,
            metadata_fingerprint TEXT NOT NULL,
            platform_metadata_json TEXT NOT NULL DEFAULT '{}',
            adapter_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_id TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL CHECK (platform IN ('bilibili', 'xiaohongshu', 'douyin')),
            platform_collection_id TEXT NOT NULL,
            name TEXT NOT NULL,
            source_url TEXT,
            adapter_version TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (platform, platform_collection_id)
        )
        """,
        """
        CREATE TABLE item_collections (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            state TEXT NOT NULL CHECK (state IN ('active', 'removed')),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            removed_at TEXT,
            last_complete_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
            PRIMARY KEY (item_id, collection_id),
            CHECK (
                (state = 'active' AND removed_at IS NULL)
                OR (state = 'removed' AND removed_at IS NOT NULL)
            )
        )
        """,
        """
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL UNIQUE,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            kind TEXT NOT NULL,
            source_url TEXT,
            local_path TEXT,
            sha256 TEXT,
            mime_type TEXT,
            size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
            quality TEXT,
            cleanup_status TEXT NOT NULL DEFAULT 'retained',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (item_id, kind, ordinal)
        )
        """,
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
            platform TEXT NOT NULL CHECK (platform IN ('bilibili', 'xiaohongshu', 'douyin')),
            stage TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'running', 'succeeded', 'retryable',
                    'needs_auth', 'blocked', 'failed'
                )
            ),
            idempotency_key TEXT NOT NULL UNIQUE,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            last_diagnostic_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (item_id, stage, idempotency_key)
        )
        """,
        """
        CREATE TABLE extractions (
            id TEXT PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
            extraction_type TEXT NOT NULL,
            processor_version TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            result_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (extraction_type, input_fingerprint, processor_version, config_hash)
        )
        """,
        """
        CREATE TABLE enrichments (
            id TEXT PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (item_id, model, prompt_version, input_hash)
        )
        """,
    ),
    2: (
        "CREATE INDEX idx_items_platform_last_seen ON items(platform, last_seen_at)",
        "CREATE INDEX idx_item_collections_state ON item_collections(collection_id, state)",
        "CREATE INDEX idx_assets_sha256 ON assets(sha256) WHERE sha256 IS NOT NULL",
        "CREATE INDEX idx_jobs_ready ON jobs(status, next_attempt_at, lease_until)",
        "CREATE INDEX idx_jobs_item_stage ON jobs(item_id, stage)",
        "CREATE INDEX idx_extractions_item_type ON extractions(item_id, extraction_type)",
    ),
}

