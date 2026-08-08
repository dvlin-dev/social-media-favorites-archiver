from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from social_media_favorites_archiver.adapters.base import FavoriteRef, SessionState, SessionStatus
from social_media_favorites_archiver.adapters.xiaohongshu import XiaohongshuAdapter
from social_media_favorites_archiver.models import ContentBlockKind, Platform
from social_media_favorites_archiver.storage.assets import StoredAsset
from social_media_favorites_archiver.storage.markdown import MarkdownRenderer, NoteRenderContext

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sanitized" / "xiaohongshu.json"
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


class FixtureAssetStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def download_static(self, url: str, **kwargs: object) -> StoredAsset:
        del url
        asset_id = str(kwargs["asset_id"])
        ordinal = int(str(kwargs["ordinal"]))
        dimensions = (4, 3) if ordinal == 0 else (2, 2)
        path = self.root / f"{asset_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        Image.new("RGB", dimensions, "white").save(buffer, format="PNG")
        payload = buffer.getvalue()
        path.write_bytes(payload)
        import hashlib

        return StoredAsset(
            path=path,
            sha256=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            size_bytes=len(payload),
            mime_type="image/png",
        )


def _fixture() -> dict[str, object]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _reference(item_id: str) -> FavoriteRef:
    return FavoriteRef(
        canonical_id=f"xiaohongshu:{item_id}",
        platform=Platform.XIAOHONGSHU,
        platform_item_id=item_id,
        source_url=f"https://www.xiaohongshu.com/explore/{item_id}",
    )


def _adapter(tmp_path: Path | None = None) -> XiaohongshuAdapter:
    return XiaohongshuAdapter(
        bridge=FixtureBridge(_fixture()),
        asset_store=None if tmp_path is None else FixtureAssetStore(tmp_path / "assets"),
        now=lambda: NOW,
    )


def test_ordered_gallery_prefers_default_url_and_records_page_renditions(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    item = asyncio.run(adapter.fetch_item(_reference("fixture-gallery")))

    assert [asset.ordinal for asset in item.assets] == [0, 1]
    assert item.assets[0].source_url == "https://example.invalid/original-1.png"
    assert item.assets[0].quality == "page-default"
    assert item.assets[1].quality == "fallback"
    assert item.platform_metadata["image_quality_downgrades"] == [
        item.assets[0].asset_id,
        item.assets[1].asset_id,
    ]
    assert [block.kind for block in item.content_blocks] == [
        ContentBlockKind.TEXT,
        ContentBlockKind.ASSET,
        ContentBlockKind.ASSET,
    ]

    downloaded = asyncio.run(adapter.download_assets(item, tmp_path / "unused"))
    assert [asset.local_path is not None for asset in downloaded] == [True, True]
    assert all(asset.size_bytes is not None and asset.size_bytes > 0 for asset in downloaded)


def test_gallery_markdown_keeps_ocr_immediately_below_each_image(tmp_path: Path) -> None:
    item = asyncio.run(_adapter().fetch_item(_reference("fixture-gallery")))
    rendered = MarkdownRenderer(tmp_path / "vault").render(
        item,
        NoteRenderContext(
            processing_status="complete",
            first_synced_at=NOW,
            last_synced_at=NOW,
            image_ocr={
                item.assets[0].asset_id: ("OCR first",),
                item.assets[1].asset_id: ("OCR second",),
            },
        ),
    )
    body = rendered.path.read_text(encoding="utf-8")

    first_image = body.index(f"![{item.assets[0].asset_id}]")
    first_ocr = body.index("OCR first")
    second_image = body.index(f"![{item.assets[1].asset_id}]")
    second_ocr = body.index("OCR second")
    assert first_image < first_ocr < second_image < second_ocr


def test_video_defaults_to_local_asr_and_adaptive_frame_ocr() -> None:
    item = asyncio.run(_adapter().fetch_item(_reference("fixture-video")))

    assert item.native_subtitles == ()
    assert item.platform_metadata["subtitle_probe"] == "not_exposed"
    assert item.platform_metadata["requires_local_asr"] is True
    assert item.platform_metadata["requires_adaptive_frame_ocr"] is True


def test_committed_fixture_contains_shapes_but_no_live_private_values() -> None:
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    fixture = json.loads(raw)

    assert fixture["evidence"]["values"] == "synthetic-placeholders-only"
    assert "xsecToken" in fixture["evidence"]["observed_shapes"]["favorite_entry"]
    forbidden = ("cookie", "authorization", "xsec_token", "signature", "xiaohongshu.com/explore/")
    assert all(value not in raw.lower() for value in forbidden)
