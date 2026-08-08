from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    Collection,
    ContentBlock,
    ContentBlockKind,
    ContentType,
    ExtractionRecord,
    ExtractionType,
    ItemCollectionMembership,
    MembershipState,
    NormalizedItem,
    Platform,
    SourceAvailability,
    TextSegment,
    TextSource,
    make_canonical_id,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SHA_A = f"sha256:{'a' * 64}"
SHA_B = f"sha256:{'b' * 64}"


def test_canonical_platform_ids_are_prefixed_and_stable() -> None:
    assert make_canonical_id(Platform.BILIBILI, "BV1example") == "bilibili:BV1example"
    assert make_canonical_id(Platform.XIAOHONGSHU, "note-001") == "xiaohongshu:note-001"

    with pytest.raises(ValueError):
        make_canonical_id(Platform.DOUYIN, "")


def test_collection_preserves_platform_identity() -> None:
    collection = Collection(
        platform=Platform.BILIBILI,
        platform_collection_id="fav-001",
        name="Test collection",
        source_url="https://www.bilibili.com/medialist/play/fav-001",
    )

    assert collection.canonical_id == "bilibili:collection:fav-001"


def test_normalized_item_preserves_ordered_assets_and_blocks() -> None:
    assets = (
        Asset(asset_id="asset-1", ordinal=0, kind=AssetKind.IMAGE, sha256=SHA_A),
        Asset(asset_id="asset-2", ordinal=1, kind=AssetKind.IMAGE, sha256=SHA_B),
    )
    blocks = (
        ContentBlock(ordinal=0, kind=ContentBlockKind.TEXT, text="Example text"),
        ContentBlock(ordinal=1, kind=ContentBlockKind.ASSET, asset_id="asset-1"),
    )
    item = NormalizedItem(
        canonical_id="xiaohongshu:note-001",
        platform=Platform.XIAOHONGSHU,
        content_type=ContentType.IMAGE_POST,
        source_url="https://www.xiaohongshu.com/explore/note-001",
        title="Example",
        author="Example author",
        published_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        source_availability=SourceAvailability.AVAILABLE,
        original_text="Example text",
        assets=assets,
        content_blocks=blocks,
        metadata_fingerprint=SHA_A,
        adapter_version="fixture-v1",
    )

    assert [asset.asset_id for asset in item.assets] == ["asset-1", "asset-2"]
    assert [block.ordinal for block in item.content_blocks] == [0, 1]

    with pytest.raises(ValidationError):
        NormalizedItem.model_validate(
            {**item.model_dump(), "assets": tuple(reversed(assets))}
        )


def test_item_rejects_cross_platform_identity_and_sensitive_raw_metadata() -> None:
    base = {
        "canonical_id": "douyin:123456",
        "platform": Platform.XIAOHONGSHU,
        "content_type": ContentType.VIDEO,
        "source_url": "https://example.invalid/item",
        "title": "Example",
        "author": "Example author",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "metadata_fingerprint": SHA_A,
        "adapter_version": "fixture-v1",
    }
    with pytest.raises(ValidationError):
        NormalizedItem.model_validate(base)

    base["canonical_id"] = "xiaohongshu:note-001"
    base["platform_metadata"] = {"authorization": "Bearer fake-secret"}
    with pytest.raises(ValidationError):
        NormalizedItem.model_validate(base)


def test_text_segments_validate_timeline_and_provenance() -> None:
    segment = TextSegment(
        segment_id="segment-1",
        start_time=1.25,
        end_time=2.5,
        text="Example transcript",
        raw_text="Example transcript",
        source=TextSource.ASR,
        confidence=0.92,
        asset_id="asset-audio",
    )
    assert segment.duration == 1.25

    with pytest.raises(ValidationError):
        TextSegment(
            segment_id="bad",
            start_time=3,
            end_time=2,
            text="Invalid",
            source=TextSource.OCR,
        )


def test_extraction_record_separates_processor_and_asset_fingerprints() -> None:
    record = ExtractionRecord(
        extraction_id="extract-1",
        canonical_id="bilibili:BV1example",
        extraction_type=ExtractionType.ASR,
        processor_version="asr-v1",
        input_fingerprint=SHA_A,
        config_hash=SHA_B,
        result_hash=SHA_A,
        created_at=NOW,
    )

    assert record.input_fingerprint != record.config_hash


def test_membership_removal_requires_removed_at() -> None:
    active = ItemCollectionMembership(
        canonical_id="bilibili:BV1example",
        collection_canonical_id="bilibili:collection:fav-001",
        state=MembershipState.ACTIVE,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    assert active.removed_at is None

    with pytest.raises(ValidationError):
        ItemCollectionMembership(
            canonical_id=active.canonical_id,
            collection_canonical_id=active.collection_canonical_id,
            state=MembershipState.REMOVED,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
