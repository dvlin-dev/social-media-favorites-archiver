import hashlib
from pathlib import Path

import pytest

from social_media_favorites_archiver.safety.paths import (
    AssetPathManager,
    PathEscapeError,
    resolve_within,
    safe_asset_filename,
)
from social_media_favorites_archiver.storage.assets import (
    AssetIntegrityError,
    AssetStore,
    CacheGuard,
    DiskPressureError,
)


def test_resolve_within_rejects_traversal_and_absolute_escape(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()

    with pytest.raises(PathEscapeError):
        resolve_within(root, "../outside.txt")
    with pytest.raises(PathEscapeError):
        resolve_within(root, tmp_path / "outside.txt")


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapeError):
        resolve_within(root, root / "link" / "private.bin")


def test_asset_filenames_are_bounded_valid_and_collision_resistant() -> None:
    first = safe_asset_filename("A/B:*? very long " * 30, "asset-1", ".jpg", ordinal=0)
    second = safe_asset_filename("A/B:*? very long " * 30, "asset-2", ".jpg", ordinal=0)

    assert len(first.encode("utf-8")) <= 180
    assert not any(character in first for character in '/\\:*?"<>|')
    assert first.endswith(".jpg")
    assert first != second

    with pytest.raises(ValueError):
        safe_asset_filename("title", "asset", "../../bad", ordinal=0)


def test_cross_item_asset_ownership_is_rejected(tmp_path: Path) -> None:
    manager = AssetPathManager(tmp_path / "cache")
    item_a = "bilibili:BV1example"
    item_b = "douyin:123456"
    path_b = manager.allocate(item_b, "video", "asset-b", ".mp4", ordinal=0)

    with pytest.raises(PathEscapeError):
        manager.assert_owned(item_a, path_b)


def test_atomic_stream_write_validates_mime_size_and_sha(tmp_path: Path) -> None:
    payload = b"redistributable-test-bytes"
    expected_sha = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    store = AssetStore(
        AssetPathManager(tmp_path / "cache"),
        max_asset_bytes=1024,
        allowed_mime_types={"image/jpeg"},
    )

    stored = store.save_stream(
        canonical_id="xiaohongshu:note-001",
        asset_id="image-1",
        title="Example image",
        extension=".jpg",
        ordinal=0,
        chunks=(payload[:5], payload[5:]),
        mime_type="image/jpeg",
        expected_size=len(payload),
        expected_sha256=expected_sha,
    )

    assert stored.path.read_bytes() == payload
    assert stored.sha256 == expected_sha
    assert not list(stored.path.parent.glob("*.partial"))

    with pytest.raises(AssetIntegrityError):
        store.save_stream(
            canonical_id="xiaohongshu:note-001",
            asset_id="image-mime",
            title="Wrong MIME",
            extension=".jpg",
            ordinal=1,
            chunks=(payload,),
            mime_type="text/html",
        )
    with pytest.raises(AssetIntegrityError):
        store.save_stream(
            canonical_id="xiaohongshu:note-001",
            asset_id="image-size",
            title="Too large",
            extension=".jpg",
            ordinal=2,
            chunks=(payload,),
            mime_type="image/jpeg",
            expected_size=2048,
        )
    with pytest.raises(AssetIntegrityError):
        store.save_stream(
            canonical_id="xiaohongshu:note-001",
            asset_id="image-2",
            title="Bad image",
            extension=".jpg",
            ordinal=3,
            chunks=(payload,),
            mime_type="image/jpeg",
            expected_sha256=f"sha256:{'0' * 64}",
        )


def test_cache_guard_pauses_before_quota_is_exceeded(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "existing.bin").write_bytes(b"12345678")
    guard = CacheGuard(cache, quota_bytes=10, reserve_bytes=0)

    with pytest.raises(DiskPressureError):
        guard.assert_can_allocate(3)
