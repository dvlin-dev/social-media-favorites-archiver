from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from social_media_favorites_archiver.adapters.base import FavoriteRef, SessionState, SessionStatus
from social_media_favorites_archiver.adapters.douyin import DouyinAdapter
from social_media_favorites_archiver.models import (
    ContentBlockKind,
    Platform,
    TextSegment,
    TextSource,
)
from social_media_favorites_archiver.processors.fusion import FusionKind
from social_media_favorites_archiver.storage.markdown import MarkdownRenderer, NoteRenderContext

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sanitized" / "douyin.json"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FixtureBridge:
    def __init__(self, fixture: dict[str, object]) -> None:
        self.fixture = fixture

    async def check_session(self) -> SessionStatus:
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def collection_page(self, cursor: str | None) -> dict[str, object]:
        pages = self.fixture["collection_pages"]
        assert isinstance(pages, dict)
        return pages[cursor or "root"]

    async def favorite_page(
        self,
        collection_id: str,
        cursor: str | None,
    ) -> dict[str, object]:
        del collection_id
        pages = self.fixture["favorite_pages"]
        assert isinstance(pages, dict)
        return pages[cursor or "root"]

    async def item_detail(self, item_id: str) -> dict[str, object]:
        details = self.fixture["details"]
        assert isinstance(details, dict)
        return details[item_id]


def _fixture() -> dict[str, object]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _adapter() -> DouyinAdapter:
    return DouyinAdapter(bridge=FixtureBridge(_fixture()), now=lambda: NOW)


def _reference(item_id: str) -> FavoriteRef:
    path = "note" if item_id == "fixture-gallery" else "video"
    return FavoriteRef(
        canonical_id=f"douyin:{item_id}",
        platform=Platform.DOUYIN,
        platform_item_id=item_id,
        source_url=f"https://www.douyin.com/{path}/{item_id}",
    )


def test_image_gallery_stays_ordered_and_renders_inline_ocr(tmp_path: Path) -> None:
    item = asyncio.run(_adapter().fetch_item(_reference("fixture-gallery")))

    assert [asset.ordinal for asset in item.assets] == [0, 1]
    assert [asset.quality for asset in item.assets] == ["page-high", "fallback"]
    assert [block.kind for block in item.content_blocks] == [
        ContentBlockKind.TEXT,
        ContentBlockKind.ASSET,
        ContentBlockKind.ASSET,
    ]
    rendered = MarkdownRenderer(tmp_path / "vault").render(
        item,
        NoteRenderContext(
            processing_status="complete",
            first_synced_at=NOW,
            last_synced_at=NOW,
            image_ocr={
                item.assets[0].asset_id: ("OCR one",),
                item.assets[1].asset_id: ("OCR two",),
            },
        ),
    )
    body = rendered.path.read_text(encoding="utf-8")
    assert body.index("OCR one") < body.index(f"![{item.assets[1].asset_id}]")
    assert body.index(f"![{item.assets[1].asset_id}]") < body.index("OCR two")


def test_spoken_caption_and_visual_label_flow_through_timeline_fusion() -> None:
    asr = (
        TextSegment(
            segment_id="asr-1",
            start_time=0,
            end_time=2,
            text="打开设置",
            source=TextSource.ASR,
        ),
    )
    ocr = (
        TextSegment(
            segment_id="caption-1",
            start_time=0.1,
            end_time=1.9,
            text="打开设置",
            source=TextSource.BURNED_CAPTION,
        ),
        TextSegment(
            segment_id="label-1",
            start_time=1,
            end_time=1,
            text="版本 2.0",
            source=TextSource.VISUAL_ANNOTATION,
        ),
    )

    result = _adapter().fuse_video_text(asr, ocr)

    assert [segment.kind for segment in result.segments] == [
        FusionKind.SPOKEN,
        FusionKind.VISUAL,
    ]
    assert result.transcript.count("打开设置") == 1
    assert "版本 2.0" in result.transcript


def test_committed_fixture_is_structural_and_contains_no_live_private_values() -> None:
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    fixture = json.loads(raw)

    assert fixture["evidence"]["values"] == "synthetic-placeholders-only"
    forbidden = (
        "cookie",
        "authorization",
        "a_bogus",
        "signature",
        "douyin.com/video/",
        "douyin.com/note/",
    )
    assert all(value not in raw.lower() for value in forbidden)
