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
)
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


def test_adapter_diagnostic_never_includes_raw_exception_text() -> None:
    diagnostic = asyncio.run(
        _adapter().diagnose(RuntimeError("Cookie: fake-private-cookie"))
    )

    assert "fake-private-cookie" not in diagnostic.model_dump_json()
    assert diagnostic.code == "xiaohongshu.unexpected"
