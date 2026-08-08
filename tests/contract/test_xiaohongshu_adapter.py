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
from social_media_favorites_archiver.adapters.xiaohongshu import (
    XiaohongshuAdapter,
    XiaohongshuBrowserBridge,
    _dimensions_match,
    _image_url,
    _video_url,
)
from social_media_favorites_archiver.browser.interception import PageContextClient
from social_media_favorites_archiver.models import ContentType, Platform, SourceAvailability

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sanitized" / "xiaohongshu.json"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FixtureBridge:
    def __init__(self, fixture: dict[str, object], *, authenticated: bool = True) -> None:
        self.fixture = fixture
        self.authenticated = authenticated

    async def check_session(self) -> SessionStatus:
        return SessionStatus(
            state=SessionState.AUTHENTICATED if self.authenticated else SessionState.EXPIRED,
            diagnostic_code=None if self.authenticated else "xiaohongshu.session_expired",
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


class DelayedSessionPage:
    def __init__(self) -> None:
        self.evaluations = 0
        self.handlers: dict[str, object] = {}

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.url = url
        self.wait_until = wait_until

    async def evaluate(self, expression: str, arg: object | None = None) -> object:
        del expression, arg
        self.evaluations += 1
        if self.evaluations == 1:
            return {"authenticated": False, "profile_path": None}
        return {"authenticated": True, "profile_path": "/user/profile/fixture-user"}

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler


def _fixture() -> dict[str, object]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _adapter(*, authenticated: bool = True) -> XiaohongshuAdapter:
    return XiaohongshuAdapter(
        bridge=FixtureBridge(_fixture(), authenticated=authenticated),
        now=lambda: NOW,
    )


def _reference(item_id: str) -> FavoriteRef:
    return FavoriteRef(
        canonical_id=f"xiaohongshu:{item_id}",
        platform=Platform.XIAOHONGSHU,
        platform_item_id=item_id,
        source_url=f"https://www.xiaohongshu.com/explore/{item_id}",
    )


def test_collection_and_favorite_cursor_pagination_is_complete_and_stable() -> None:
    async def exercise() -> None:
        adapter = _adapter()
        first_collections = await adapter.list_collections()
        second_collections = await adapter.list_collections(first_collections.next_cursor)
        assert first_collections.complete is False
        assert second_collections.complete is True

        first = await adapter.list_favorites(first_collections.items[0])
        second = await adapter.list_favorites(first_collections.items[0], first.next_cursor)
        assert [item.canonical_id for item in (*first.items, *second.items)] == [
            "xiaohongshu:fixture-text",
            "xiaohongshu:fixture-gallery",
            "xiaohongshu:fixture-video",
            "xiaohongshu:fixture-unavailable",
        ]
        assert first.complete is False
        assert second.complete is True
        assert first.ordering_stable is True
        assert first.total_count == 4

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("item_id", "content_type", "availability"),
    (
        ("fixture-text", ContentType.ARTICLE, SourceAvailability.AVAILABLE),
        ("fixture-gallery", ContentType.GALLERY, SourceAvailability.AVAILABLE),
        ("fixture-video", ContentType.VIDEO, SourceAvailability.AVAILABLE),
        ("fixture-unavailable", ContentType.ARTICLE, SourceAvailability.UNAVAILABLE),
    ),
)
def test_pure_text_gallery_video_and_unavailable_notes_are_normalized(
    item_id: str,
    content_type: ContentType,
    availability: SourceAvailability,
) -> None:
    item = asyncio.run(_adapter().fetch_item(_reference(item_id)))

    assert item.content_type == content_type
    assert item.source_availability == availability
    assert item.canonical_id == f"xiaohongshu:{item_id}"


def test_session_expiry_and_layout_drift_pause_safely() -> None:
    assert asyncio.run(_adapter(authenticated=False).check_session()).state == SessionState.EXPIRED

    broken = _fixture()
    broken["favorite_pages"] = {"root": {"unexpected": []}}
    adapter = XiaohongshuAdapter(bridge=FixtureBridge(broken), now=lambda: NOW)
    collection = asyncio.run(adapter.list_collections()).items[0]
    with pytest.raises(AdapterError) as captured:
        asyncio.run(adapter.list_favorites(collection))
    assert captured.value.code == AdapterErrorCode.LAYOUT_CHANGED


def test_browser_bridge_uses_page_context_without_python_signature_generation() -> None:
    source = inspect.getsource(XiaohongshuBrowserBridge)

    assert "PageContextClient" in source
    assert "ResponseInterceptor" in source
    assert "sign" not in source.lower()


def test_browser_bridge_waits_for_hydrated_session_state() -> None:
    async def exercise() -> None:
        page = DelayedSessionPage()
        bridge = XiaohongshuBrowserBridge(
            PageContextClient(page),
            session_poll_attempts=2,
            session_poll_interval=0,
        )

        status = await bridge.check_session()

        assert status.authenticated
        assert page.evaluations == 2

    asyncio.run(exercise())


def test_browser_bridge_preserves_card_metadata_when_detail_access_is_missing() -> None:
    async def exercise() -> None:
        bridge = XiaohongshuBrowserBridge(PageContextClient(DelayedSessionPage()))
        bridge.card_cache["fixture-unavailable"] = {
            "title": "Unavailable fixture note",
            "desc": "Fixture card text",
            "type": "normal",
            "user": {"nickname": "Fixture author"},
        }

        detail = await bridge.item_detail("fixture-unavailable")

        assert detail["noteId"] == "fixture-unavailable"
        assert detail["title"] == "Unavailable fixture note"
        assert detail["availability"] == "unavailable"
        assert detail["imageList"] == []

    asyncio.run(exercise())


def test_video_url_accepts_live_dynamic_stream_family_keys() -> None:
    detail = {
        "video": {
            "media": {
                "stream": {
                    "EF4": [
                        {
                            "defaultStream": 1,
                            "weight": 100,
                            "masterUrl": "https://example.invalid/video.mp4",
                        }
                    ]
                }
            }
        }
    }

    assert _video_url(detail) == "https://example.invalid/video.mp4"


def test_page_default_image_is_a_valid_aspect_preserving_rendition() -> None:
    url, quality = _image_url(
        {
            "urlDefault": "https://example.invalid/page-default.webp",
            "width": 4493,
            "height": 3370,
        }
    )

    assert url == "https://example.invalid/page-default.webp"
    assert quality == "page-default"
    assert _dimensions_match((1440, 1080), (4493, 3370), quality=quality)
    assert _dimensions_match((1440, 1080), (4493, 3370), quality="original")
    assert not _dimensions_match((1080, 1440), (4493, 3370), quality=quality)


def test_adapter_diagnostic_never_includes_raw_exception_text() -> None:
    diagnostic = asyncio.run(
        _adapter().diagnose(RuntimeError("Cookie: fake-private-cookie"))
    )

    assert "fake-private-cookie" not in diagnostic.model_dump_json()
    assert diagnostic.code == "xiaohongshu.unexpected"
