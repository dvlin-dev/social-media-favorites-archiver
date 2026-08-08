from datetime import UTC, datetime

from social_media_favorites_archiver.models import (
    ContentType,
    NormalizedItem,
    Platform,
    SourceAvailability,
    TextSegment,
    TextSource,
)
from social_media_favorites_archiver.worker import LocalHeavyWorker

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _item(
    identifier: str,
    *,
    availability: SourceAvailability = SourceAvailability.AVAILABLE,
    subtitles: tuple[TextSegment, ...] = (),
) -> NormalizedItem:
    return NormalizedItem(
        canonical_id=f"bilibili:{identifier}",
        platform=Platform.BILIBILI,
        content_type=ContentType.VIDEO,
        source_url=f"https://example.invalid/{identifier}",
        title="Fixture video",
        author="Fixture author",
        first_seen_at=NOW,
        last_seen_at=NOW,
        source_availability=availability,
        native_subtitles=subtitles,
        metadata_fingerprint="sha256:" + "0" * 64,
        platform_metadata={},
        adapter_version="fixture-v1",
    )


def test_bilibili_native_subtitles_skip_unnecessary_media_download() -> None:
    item = _item(
        "native-subtitle",
        subtitles=(
            TextSegment(
                segment_id="subtitle-1",
                start_time=0,
                end_time=1,
                text="Fixture subtitle",
                source=TextSource.NATIVE_SUBTITLE,
            ),
        ),
    )

    assert LocalHeavyWorker._assets_ready(item) is True


def test_unavailable_item_skips_futile_asset_download() -> None:
    item = _item(
        "unavailable",
        availability=SourceAvailability.UNAVAILABLE,
    )

    assert LocalHeavyWorker._assets_ready(item) is True
