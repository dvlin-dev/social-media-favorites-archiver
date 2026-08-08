import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from social_media_favorites_archiver.adapters.base import (
    FavoriteRef,
    SessionState,
    SessionStatus,
)
from social_media_favorites_archiver.adapters.bilibili import (
    BilibiliAdapter,
    BilibiliCollectionDiscovery,
)
from social_media_favorites_archiver.models import Collection, Platform, TextSource
from social_media_favorites_archiver.processors.subtitles import SubtitleProvenance
from social_media_favorites_archiver.storage.markdown import (
    MarkdownRenderer,
    NoteRenderContext,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sanitized" / "bilibili.json"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FixtureDiscovery(BilibiliCollectionDiscovery):
    def __init__(self, fixture) -> None:
        self.fixture = fixture

    async def check_session(self) -> SessionStatus:
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def collections(self) -> tuple[Collection, ...]:
        return tuple(
            Collection(
                platform=Platform.BILIBILI,
                platform_collection_id=entry["id"],
                name=entry["title"],
                source_url=f"https://www.bilibili.com/medialist/detail/ml{entry['id']}",
                adapter_version="fixture-v1",
            )
            for entry in self.fixture["collections"]
        )


class FixtureBridge:
    def __init__(self, fixture) -> None:
        self.fixture = fixture

    def extract(self, url: str, *, flat: bool = False):
        if "medialist" in url:
            return self.fixture["playlist"]
        item_id = url.rstrip("/").rsplit("/", 1)[-1]
        return self.fixture["items"][item_id]

    def download(self, url: str, output_dir: Path) -> tuple[Path, ...]:
        return ()


def _adapter() -> BilibiliAdapter:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return BilibiliAdapter(
        bridge=FixtureBridge(fixture),
        discovery=FixtureDiscovery(fixture),
        now=lambda: NOW,
    )


def _reference(item_id: str) -> FavoriteRef:
    return FavoriteRef(
        canonical_id=f"bilibili:{item_id}",
        platform=Platform.BILIBILI,
        platform_item_id=item_id,
        source_url=f"https://www.bilibili.com/video/{item_id}",
    )


def test_native_subtitle_is_normalized_with_provenance_and_skips_asr() -> None:
    item = asyncio.run(_adapter().fetch_item(_reference("BV1fixture")))

    assert len(item.native_subtitles) == 1
    assert item.native_subtitles[0].source == TextSource.NATIVE_SUBTITLE
    assert SubtitleProvenance.HUMAN.value in item.native_subtitles[0].provenance
    assert item.platform_metadata["requires_local_asr"] is False


def test_missing_subtitle_schedules_local_asr_without_failing_item() -> None:
    item = asyncio.run(_adapter().fetch_item(_reference("BV1nosub")))

    assert item.native_subtitles == ()
    assert item.platform_metadata["requires_local_asr"] is True


def test_multi_part_order_renders_as_chapters_in_one_primary_note(tmp_path: Path) -> None:
    item = asyncio.run(_adapter().fetch_item(_reference("BV1multi")))
    renderer = MarkdownRenderer(tmp_path)

    result = renderer.render(
        item,
        NoteRenderContext(
            collections=("Fixture collection",),
            processing_status="complete",
            first_synced_at=NOW,
            last_synced_at=NOW,
            transcript=item.native_subtitles,
        ),
    )
    body = result.path.read_text(encoding="utf-8")

    assert [part["title"] for part in item.platform_metadata["parts"]] == [
        "Part one",
        "Part two",
    ]
    assert body.index("Part one") < body.index("Part two")
    assert len(list((tmp_path / "Bilibili").glob("*.md"))) == 1
    assert [segment.start_time for segment in item.native_subtitles] == [0.0, 11.0]
