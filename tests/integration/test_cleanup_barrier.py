import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    ContentType,
    NormalizedItem,
    Platform,
)
from social_media_favorites_archiver.safety.cleanup import (
    BarrierState,
    CleanupManager,
    DerivativeBarrier,
)
from social_media_favorites_archiver.safety.paths import AssetPathManager, PathEscapeError
from social_media_favorites_archiver.storage.database import Database

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
METADATA_SHA = f"sha256:{'a' * 64}"


def _ready_barrier(**updates) -> DerivativeBarrier:
    values = {
        "speech_extraction": BarrierState.SUCCEEDED,
        "keyframes": BarrierState.SUCCEEDED,
        "ocr": BarrierState.SUCCEEDED,
        "fusion": BarrierState.SUCCEEDED,
        "markdown_render": BarrierState.SUCCEEDED,
        "retained_assets": BarrierState.SUCCEEDED,
        "final_verification": BarrierState.SUCCEEDED,
    }
    values.update(updates)
    return DerivativeBarrier.model_validate(values)


def _registered_video(tmp_path: Path) -> tuple[CleanupManager, Database, Path, str]:
    cache_root = tmp_path / "cache"
    paths = AssetPathManager(cache_root)
    database = Database(tmp_path / "archive.db")
    database.migrate()
    canonical_id = "bilibili:BV1example"
    item_id = database.upsert_item(
        NormalizedItem(
            canonical_id=canonical_id,
            platform=Platform.BILIBILI,
            content_type=ContentType.VIDEO,
            source_url="https://www.bilibili.com/video/BV1example",
            title="Example",
            author="Example author",
            first_seen_at=NOW,
            last_seen_at=NOW,
            metadata_fingerprint=METADATA_SHA,
            adapter_version="fixture-v1",
        )
    )
    video_path = paths.allocate(canonical_id, "temporary video", "video-1", ".mp4", ordinal=0)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"temporary-video-fixture"
    video_path.write_bytes(payload)
    video_sha = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    database.upsert_asset(
        item_id,
        Asset(
            asset_id="video-1",
            ordinal=0,
            kind=AssetKind.VIDEO,
            local_path=video_path,
            sha256=video_sha,
        ),
    )
    return CleanupManager(database, paths), database, video_path, canonical_id


@pytest.mark.parametrize(
    "blocked_stage",
    (
        "speech_extraction",
        "keyframes",
        "ocr",
        "fusion",
        "markdown_render",
        "retained_assets",
        "final_verification",
    ),
)
def test_video_is_retained_until_every_derivative_succeeds(
    tmp_path: Path,
    blocked_stage: str,
) -> None:
    manager, _, video_path, canonical_id = _registered_video(tmp_path)
    barrier = _ready_barrier(**{blocked_stage: BarrierState.FAILED})

    result = manager.cleanup(canonical_id, video_path, barrier=barrier)

    assert result.status == "retained"
    assert blocked_stage in result.blocking_stages
    assert video_path.exists()


def test_cleanup_is_dry_runnable_verified_idempotent_and_auditable(tmp_path: Path) -> None:
    manager, database, video_path, canonical_id = _registered_video(tmp_path)
    barrier = _ready_barrier()

    preview = manager.cleanup(canonical_id, video_path, barrier=barrier, dry_run=True)
    assert preview.status == "eligible"
    assert video_path.exists()

    cleaned = manager.cleanup(canonical_id, video_path, barrier=barrier)
    assert cleaned.status == "cleaned"
    assert cleaned.sha256_verified is True
    assert not video_path.exists()

    repeated = manager.cleanup(canonical_id, video_path, barrier=barrier)
    assert repeated.status == "already_cleaned"
    with database.connect() as connection:
        status = connection.execute(
            "SELECT cleanup_status FROM assets WHERE local_path = ?",
            (str(video_path),),
        ).fetchone()[0]
    assert status == "cleaned"


def test_cleanup_refuses_unregistered_or_cross_item_paths(tmp_path: Path) -> None:
    manager, _, video_path, _ = _registered_video(tmp_path)

    with pytest.raises(PathEscapeError):
        manager.cleanup("douyin:123456", video_path, barrier=_ready_barrier())

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"do-not-delete")
    with pytest.raises(PathEscapeError):
        manager.cleanup("bilibili:BV1example", outside, barrier=_ready_barrier())
    assert outside.exists()
