"""Verified, item-owned temporary-media cleanup."""

from __future__ import annotations

import hashlib
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from social_media_favorites_archiver.queue import ItemFileLocks
from social_media_favorites_archiver.safety.paths import AssetPathManager, PathEscapeError
from social_media_favorites_archiver.storage.database import Database


class BarrierState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    NOT_REQUIRED = "not_required"
    FAILED = "failed"


class DerivativeBarrier(BaseModel):
    """Every derivative and final-file gate required before video deletion."""

    model_config = ConfigDict(frozen=True)

    speech_extraction: BarrierState = BarrierState.PENDING
    keyframes: BarrierState = BarrierState.PENDING
    ocr: BarrierState = BarrierState.PENDING
    fusion: BarrierState = BarrierState.PENDING
    markdown_render: BarrierState = BarrierState.PENDING
    retained_assets: BarrierState = BarrierState.PENDING
    final_verification: BarrierState = BarrierState.PENDING

    @property
    def blocking_stages(self) -> tuple[str, ...]:
        completed = {BarrierState.SUCCEEDED, BarrierState.NOT_REQUIRED}
        return tuple(
            name
            for name in type(self).model_fields
            if getattr(self, name) not in completed
        )

    @property
    def ready(self) -> bool:
        return not self.blocking_stages


class CleanupResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["retained", "eligible", "cleaned", "already_cleaned"]
    blocking_stages: tuple[str, ...] = ()
    sha256_verified: bool = False
    diagnostic_code: str


class CleanupVerificationError(RuntimeError):
    """Raised when a registered file cannot be verified safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class CleanupManager:
    """Delete one exact registered temporary file after every gate succeeds."""

    def __init__(self, database: Database, paths: AssetPathManager) -> None:
        self.database = database
        self.paths = paths
        self.file_locks = ItemFileLocks(paths.cache_root / ".cleanup-locks")

    def _registered_asset(self, canonical_id: str, path: Path) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT assets.id, assets.sha256, assets.cleanup_status
                FROM assets
                JOIN items ON items.id = assets.item_id
                WHERE items.canonical_id = ? AND assets.local_path = ?
                """,
                (canonical_id, str(path)),
            ).fetchone()
        if row is None:
            raise PathEscapeError("file is not registered to the canonical item")
        return cast(sqlite3.Row, row)

    def cleanup(
        self,
        canonical_id: str,
        candidate: str | Path,
        *,
        barrier: DerivativeBarrier,
        dry_run: bool = False,
    ) -> CleanupResult:
        path = self.paths.assert_owned(canonical_id, candidate)
        with self.file_locks.acquire(canonical_id):
            row = self._registered_asset(canonical_id, path)
            cleanup_status = str(row["cleanup_status"])
            expected_sha256 = row["sha256"]
            if cleanup_status == "cleaned":
                return CleanupResult(
                    status="already_cleaned",
                    sha256_verified=True,
                    diagnostic_code="cleanup.already_cleaned",
                )
            if not barrier.ready:
                return CleanupResult(
                    status="retained",
                    blocking_stages=barrier.blocking_stages,
                    diagnostic_code="cleanup.barrier_blocked",
                )
            if not path.is_file() or path.is_symlink():
                raise CleanupVerificationError("registered temporary file is missing or unsafe")
            actual_sha256 = _sha256(path)
            if not isinstance(expected_sha256, str) or actual_sha256 != expected_sha256:
                raise CleanupVerificationError("registered temporary file hash does not match")
            if dry_run:
                return CleanupResult(
                    status="eligible",
                    sha256_verified=True,
                    diagnostic_code="cleanup.dry_run",
                )
            path.unlink()
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE assets SET cleanup_status = 'cleaned' WHERE id = ?",
                    (row["id"],),
                )
            return CleanupResult(
                status="cleaned",
                sha256_verified=True,
                diagnostic_code="cleanup.cleaned",
            )
