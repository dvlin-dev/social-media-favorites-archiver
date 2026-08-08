"""Atomic, bounded asset storage for adapter-obtained static media."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from social_media_favorites_archiver.safety.paths import AssetPathManager


class AssetIntegrityError(ValueError):
    """Raised when MIME, size, hash, or collision checks fail."""


class DiskPressureError(RuntimeError):
    """Raised when a heavy write must pause for quota or free-space safety."""


class StoredAsset(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    sha256: str
    size_bytes: int
    mime_type: str


class CacheGuard:
    """Bound application-owned cache usage and preserve filesystem reserve."""

    def __init__(
        self,
        cache_root: str | Path,
        *,
        quota_bytes: int,
        reserve_bytes: int = 1024**3,
    ) -> None:
        if quota_bytes < 1 or reserve_bytes < 0:
            msg = "cache quota must be positive and reserve must be non-negative"
            raise ValueError(msg)
        self.cache_root = Path(cache_root).resolve(strict=False)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.quota_bytes = quota_bytes
        self.reserve_bytes = reserve_bytes

    def usage_bytes(self) -> int:
        total = 0
        for path in self.cache_root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        return total

    def assert_can_allocate(self, size_bytes: int) -> None:
        if size_bytes < 0:
            msg = "allocation size must be non-negative"
            raise ValueError(msg)
        if self.usage_bytes() + size_bytes > self.quota_bytes:
            raise DiskPressureError("configured cache quota would be exceeded")
        free = shutil.disk_usage(self.cache_root).free
        if free - size_bytes < self.reserve_bytes:
            raise DiskPressureError("filesystem free-space reserve would be exceeded")


class AssetStore:
    """Store verified streams without exposing temporary partial data."""

    def __init__(
        self,
        paths: AssetPathManager,
        *,
        max_asset_bytes: int,
        allowed_mime_types: set[str],
        cache_guard: CacheGuard | None = None,
    ) -> None:
        if max_asset_bytes < 1 or not allowed_mime_types:
            msg = "asset size and MIME allowlist must be configured"
            raise ValueError(msg)
        self.paths = paths
        self.max_asset_bytes = max_asset_bytes
        self.allowed_mime_types = frozenset(allowed_mime_types)
        self.cache_guard = cache_guard

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stored:
            for chunk in iter(lambda: stored.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    def save_stream(
        self,
        *,
        canonical_id: str,
        asset_id: str,
        title: str,
        extension: str,
        ordinal: int,
        chunks: Iterable[bytes],
        mime_type: str,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredAsset:
        normalized_mime = mime_type.split(";", 1)[0].strip().lower()
        if normalized_mime not in self.allowed_mime_types:
            raise AssetIntegrityError("asset MIME type is not allowed")
        if expected_size is not None and expected_size > self.max_asset_bytes:
            raise AssetIntegrityError("expected asset size exceeds the configured maximum")
        if self.cache_guard is not None:
            self.cache_guard.assert_can_allocate(expected_size or 0)

        target = self.paths.allocate(
            canonical_id,
            title,
            asset_id,
            extension,
            ordinal=ordinal,
        )
        self.paths.assert_owned(canonical_id, target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            existing_hash = self._sha256(target)
            if expected_sha256 is not None and existing_hash == expected_sha256:
                return StoredAsset(
                    path=target,
                    sha256=existing_hash,
                    size_bytes=target.stat().st_size,
                    mime_type=normalized_mime,
                )
            raise AssetIntegrityError("asset target already exists with unverified content")

        partial = self.paths.assert_owned(canonical_id, target.with_name(f"{target.name}.partial"))
        partial.unlink(missing_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("xb") as output:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AssetIntegrityError("asset stream yielded a non-bytes chunk")
                    size += len(chunk)
                    if size > self.max_asset_bytes:
                        raise AssetIntegrityError("asset exceeds the configured maximum size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            actual_sha256 = f"sha256:{digest.hexdigest()}"
            if expected_size is not None and size != expected_size:
                raise AssetIntegrityError("asset size does not match the expected size")
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise AssetIntegrityError("asset SHA-256 does not match the expected hash")
            if self.cache_guard is not None:
                self.cache_guard.assert_can_allocate(0)
            os.replace(partial, target)
            return StoredAsset(
                path=target,
                sha256=actual_sha256,
                size_bytes=size,
                mime_type=normalized_mime,
            )
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    def download_static(
        self,
        url: str,
        *,
        canonical_id: str,
        asset_id: str,
        title: str,
        extension: str,
        ordinal: int,
        expected_sha256: str | None = None,
        timeout_seconds: float = 60,
    ) -> StoredAsset:
        """Download only an adapter-obtained static URL under all storage controls."""
        with httpx.Client(follow_redirects=True, timeout=timeout_seconds) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                mime_type = response.headers.get("content-type", "")
                length_header = response.headers.get("content-length")
                expected_size = int(length_header) if length_header and length_header.isdigit() else None
                return self.save_stream(
                    canonical_id=canonical_id,
                    asset_id=asset_id,
                    title=title,
                    extension=extension,
                    ordinal=ordinal,
                    chunks=response.iter_bytes(),
                    mime_type=mime_type,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )

