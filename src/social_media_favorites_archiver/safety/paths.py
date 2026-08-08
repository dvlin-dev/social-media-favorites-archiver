"""Canonical path containment and item-owned asset locations."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a path escapes its authorized root or owner."""


def resolve_within(root: str | Path, candidate: str | Path) -> Path:
    """Resolve symlinks and require candidate to remain under root."""
    resolved_root = Path(root).expanduser().resolve(strict=False)
    candidate_path = Path(candidate).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = resolved_root / candidate_path
    resolved_candidate = candidate_path.resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_root):
        raise PathEscapeError("path escapes the authorized root")
    return resolved_candidate


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    result = value
    while len(result.encode("utf-8")) > maximum_bytes:
        result = result[:-1]
    return result


def safe_asset_filename(
    title: str,
    stable_id: str,
    extension: str,
    *,
    ordinal: int,
    maximum_bytes: int = 180,
) -> str:
    """Build a readable, bounded filename with collision-resistant identity."""
    if ordinal < 0:
        msg = "asset ordinal must be non-negative"
        raise ValueError(msg)
    if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension) is None:
        msg = "asset extension must be a simple dot-prefixed suffix"
        raise ValueError(msg)
    slug = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", title)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug).strip("-. ") or "asset"
    identity = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12]
    suffix = f"-{ordinal:03d}-{identity}{extension.lower()}"
    budget = maximum_bytes - len(suffix.encode("utf-8"))
    if budget < 1:
        msg = "maximum filename length is too small"
        raise ValueError(msg)
    slug = _truncate_utf8(slug, budget).rstrip("-. ") or "asset"
    return f"{slug}{suffix}"


class AssetPathManager:
    """Allocate and validate paths scoped to one canonical item."""

    def __init__(self, cache_root: str | Path) -> None:
        self.cache_root = Path(cache_root).expanduser().resolve(strict=False)
        self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def item_directory(self, canonical_id: str) -> Path:
        digest = hashlib.sha256(canonical_id.encode("utf-8")).hexdigest()[:24]
        return resolve_within(self.cache_root, Path("items") / digest)

    def allocate(
        self,
        canonical_id: str,
        title: str,
        asset_id: str,
        extension: str,
        *,
        ordinal: int,
    ) -> Path:
        filename = safe_asset_filename(
            title,
            asset_id,
            extension,
            ordinal=ordinal,
        )
        return resolve_within(self.item_directory(canonical_id), filename)

    def assert_owned(self, canonical_id: str, candidate: str | Path) -> Path:
        item_root = self.item_directory(canonical_id)
        resolved = resolve_within(item_root, candidate)
        if resolved == item_root:
            raise PathEscapeError("an item directory is not an owned file")
        return resolved

