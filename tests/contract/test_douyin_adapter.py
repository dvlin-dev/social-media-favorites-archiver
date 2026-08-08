from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from social_media_favorites_archiver.adapters.base import (
    AdapterError,
    AdapterErrorCode,
    FavoriteRef,
    SessionState,
    SessionStatus,
)
from social_media_favorites_archiver.adapters.douyin import DouyinAdapter, DouyinBrowserBridge
from social_media_favorites_archiver.models import ContentType, Platform, SourceAvailability

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sanitized" / "douyin.json"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FixtureBridge:
    def __init__(self, fixture: dict[str, object], *, authenticated: bool = True) -> None:
        self.fixture = fixture
        self.authenticated = authenticated

    async def check_session(self) -> SessionStatus:
        return SessionStatus(
            state=SessionState.AUTHENTICATED if self.authenticated else SessionState.EXPIRED,
            diagnostic_code=None if self.authenticated else "douyin.session_expired",
        )

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


def _adapter(*, authenticated: bool = True) -> DouyinAdapter:
    return DouyinAdapter(
        bridge=FixtureBridge(_fixture(), authenticated=authenticated),
        now=lambda: NOW,
    )


def _reference(item_id: str) -> FavoriteRef:
    return FavoriteRef(
        canonical_id=f"douyin:{item_id}",
        platform=Platform.DOUYIN,
        platform_item_id=item_id,
        source_url=f"https://www.douyin.com/video/{item_id}",
    )


def test_favorite_collection_and_item_pagination_is_complete_and_stable() -> None:
    async def exercise() -> None:
        adapter = _adapter()
        first_collections = await adapter.list_collections()
        second_collections = await adapter.list_collections(first_collections.next_cursor)
        assert first_collections.complete is False
        assert second_collections.complete is True

        first = await adapter.list_favorites(first_collections.items[0])
        second = await adapter.list_favorites(first_collections.items[0], first.next_cursor)
        assert [entry.canonical_id for entry in (*first.items, *second.items)] == [
            "douyin:fixture-track",
            "douyin:fixture-caption",
            "douyin:fixture-gallery",
            "douyin:fixture-unavailable",
        ]
        assert first.complete is False
        assert second.complete is True
        assert first.total_count == 4

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("item_id", "content_type", "availability"),
    (
        ("fixture-track", ContentType.VIDEO, SourceAvailability.AVAILABLE),
        ("fixture-caption", ContentType.VIDEO, SourceAvailability.AVAILABLE),
        ("fixture-gallery", ContentType.GALLERY, SourceAvailability.AVAILABLE),
        ("fixture-unavailable", ContentType.VIDEO, SourceAvailability.UNAVAILABLE),
    ),
)
def test_video_gallery_and_unavailable_items_are_normalized(
    item_id: str,
    content_type: ContentType,
    availability: SourceAvailability,
) -> None:
    item = asyncio.run(_adapter().fetch_item(_reference(item_id)))

    assert item.content_type == content_type
    assert item.source_availability == availability


def test_subtitle_probe_distinguishes_usable_track_from_candidate_only() -> None:
    usable = asyncio.run(_adapter().fetch_item(_reference("fixture-track")))
    candidate = asyncio.run(_adapter().fetch_item(_reference("fixture-caption")))

    assert len(usable.native_subtitles) == 1
    assert usable.platform_metadata["subtitle_probe"] == "usable"
    assert usable.platform_metadata["requires_local_asr"] is False
    assert candidate.native_subtitles == ()
    assert candidate.platform_metadata["subtitle_probe"] == "candidate_exposed"
    assert candidate.platform_metadata["requires_local_asr"] is True


def test_session_expiry_and_partial_layout_pause_safely() -> None:
    assert asyncio.run(_adapter(authenticated=False).check_session()).state == SessionState.EXPIRED
    broken = _fixture()
    broken["favorite_pages"] = {"root": {"items": [], "complete": False}}

    adapter = DouyinAdapter(bridge=FixtureBridge(broken), now=lambda: NOW)
    collection = asyncio.run(adapter.list_collections()).items[0]
    with pytest.raises(AdapterError) as captured:
        asyncio.run(adapter.list_favorites(collection))
    assert captured.value.code == AdapterErrorCode.LAYOUT_CHANGED


def test_browser_bridge_intercepts_page_responses_without_python_token_math() -> None:
    source = inspect.getsource(DouyinBrowserBridge)

    assert "PageContextClient" in source
    assert "ResponseInterceptor" in source
    assert "bogus" not in source.lower()


def test_diagnostic_does_not_include_raw_error_text() -> None:
    diagnostic = asyncio.run(_adapter().diagnose(RuntimeError("Cookie: private")))

    assert "private" not in diagnostic.model_dump_json()
    assert diagnostic.code == "douyin.unexpected"
