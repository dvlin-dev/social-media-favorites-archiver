"""Douyin favorites through authenticated page responses and local processing."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from social_media_favorites_archiver.adapters.base import (
    AdapterDiagnostic,
    AdapterError,
    AdapterErrorCode,
    BaseAdapter,
    CursorPage,
    FavoriteRef,
    LoginAction,
    LoginInstruction,
    SessionState,
    SessionStatus,
)
from social_media_favorites_archiver.browser.interception import (
    PageContextClient,
    ResponseInterceptor,
)
from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    Collection,
    ContentBlock,
    ContentBlockKind,
    ContentType,
    NormalizedItem,
    Platform,
    SourceAvailability,
    TextSegment,
    TextSource,
)
from social_media_favorites_archiver.processors.fusion import FusionResult, fuse_timelines
from social_media_favorites_archiver.storage.assets import StoredAsset

ADAPTER_VERSION = "douyin-v1"
_PROFILE_URL = "https://www.douyin.com/user/self"


class DouyinBridge(Protocol):
    async def check_session(self) -> SessionStatus: ...

    async def collection_page(self, cursor: str | None) -> dict[str, object]: ...

    async def favorite_page(
        self,
        collection_id: str,
        cursor: str | None,
    ) -> dict[str, object]: ...

    async def item_detail(self, item_id: str) -> dict[str, object]: ...


class AssetStoreLike(Protocol):
    def download_static(
        self,
        url: str,
        *,
        canonical_id: str,
        asset_id: str,
        title: str,
        extension: str,
        ordinal: int,
        expected_sha256: str | None = None,
        timeout_seconds: float = 60,
    ) -> StoredAsset: ...


def _mapping(value: object, *, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterError(AdapterErrorCode.LAYOUT_CHANGED, message, retryable=False)
    return cast(dict[str, Any], value)


def _find_page(value: object, *, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6 or not isinstance(value, dict):
        return None
    if isinstance(value.get("aweme_list"), list) and "has_more" in value:
        return cast(dict[str, Any], value)
    for nested in value.values():
        found = _find_page(nested, depth=depth + 1)
        if found is not None:
            return found
    return None


def _find_detail(value: object, *, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6 or not isinstance(value, dict):
        return None
    raw_detail = value.get("aweme_detail")
    if isinstance(raw_detail, dict):
        return cast(dict[str, Any], raw_detail)
    if (value.get("aweme_id") or value.get("id")) and (
        isinstance(value.get("video"), dict) or isinstance(value.get("images"), list)
    ):
        return cast(dict[str, Any], value)
    for nested in value.values():
        found = _find_detail(nested, depth=depth + 1)
        if found is not None:
            return found
    return None


class DouyinBrowserBridge:
    """Use the logged-in page and intercepted JSON; never synthesize page tokens."""

    _SESSION_SCRIPT = """
    async () => {
      const user = window.SSR_RENDER_DATA?.app?.user;
      return {authenticated: Boolean(user?.isLogin)};
    }
    """
    _OPEN_FAVORITES_SCRIPT = """
    async () => {
      const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      const findTarget = () => {
        const leaf = [...document.querySelectorAll('a,button,div,span')].find((node) =>
          node.children.length === 0 && node.textContent.trim() === '收藏'
        );
        return leaf ? (leaf.closest('[role="tab"],button,a') || leaf) : null;
      };
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const target = findTarget();
        if (!target) {
          await sleep(100);
          continue;
        }
        const active = target.getAttribute('aria-selected') === 'true'
          || target.getAttribute('tabindex') === '0';
        if (active) return true;
        if (attempt % 5 === 0) target.click();
        await sleep(100);
      }
      const target = findTarget();
      return Boolean(target) && (
        target.getAttribute('aria-selected') === 'true'
        || target.getAttribute('tabindex') === '0'
      );
    }
    """
    _LOAD_MORE_SCRIPT = """
    async () => {
      const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      const candidates = [...document.querySelectorAll('*')].filter((node) => {
        const style = getComputedStyle(node);
        return ['auto', 'scroll'].includes(style.overflowY)
          && node.scrollHeight > node.clientHeight + 100;
      }).sort((left, right) =>
        (right.scrollHeight - right.clientHeight) - (left.scrollHeight - left.clientHeight)
      );
      const scroller = candidates[0];
      if (!scroller) return false;
      const step = Math.max(400, Math.floor(scroller.clientHeight * 0.75));
      for (let attempt = 0; attempt < 6; attempt += 1) {
        scroller.scrollTo({
          top: Math.min(scroller.scrollHeight, scroller.scrollTop + step),
          behavior: 'smooth',
        });
        await sleep(500);
      }
      return true;
    }
    """

    def __init__(
        self,
        client: PageContextClient,
        *,
        session_poll_attempts: int = 25,
        session_poll_interval: float = 0.2,
        response_wait_seconds: float = 3.0,
    ) -> None:
        if session_poll_attempts < 1:
            raise ValueError("Douyin session poll attempts must be positive")
        if session_poll_interval < 0:
            raise ValueError("Douyin session poll interval must not be negative")
        if response_wait_seconds < 0:
            raise ValueError("Douyin response wait must not be negative")
        self.client = client
        self.session_poll_attempts = session_poll_attempts
        self.session_poll_interval = session_poll_interval
        self.response_wait_seconds = response_wait_seconds
        self._next_favorite_cursor: str | None = None
        self.favorite_responses = ResponseInterceptor(
            url_substring="/aweme/v1/web/aweme/"
        )
        self.detail_responses = ResponseInterceptor(url_substring="aweme/detail")
        self.favorite_responses.attach(client.page)
        self.detail_responses.attach(client.page)
        self.detail_cache: dict[str, dict[str, Any]] = {}

    async def check_session(self) -> SessionStatus:
        try:
            await self.client.navigate(_PROFILE_URL)
            payload: dict[str, Any] = {}
            for attempt in range(self.session_poll_attempts):
                result = await self.client.page.evaluate(self._SESSION_SCRIPT, None)
                payload = _mapping(result, message="Douyin session state changed")
                if payload.get("authenticated"):
                    break
                if attempt + 1 < self.session_poll_attempts:
                    await asyncio.sleep(self.session_poll_interval)
        except AdapterError:
            raise
        except Exception:
            return SessionStatus(
                state=SessionState.NEEDS_LOGIN,
                diagnostic_code="douyin.session_unreachable",
            )
        if not payload.get("authenticated"):
            return SessionStatus(
                state=SessionState.EXPIRED,
                diagnostic_code="douyin.session_expired",
            )
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def collection_page(self, cursor: str | None) -> dict[str, object]:
        if cursor is not None:
            raise AdapterError(
                AdapterErrorCode.ENUMERATION_INCOMPLETE,
                "Douyin collection cursor is no longer valid",
                retryable=True,
            )
        status = await self.check_session()
        if not status.authenticated:
            raise AdapterError(
                AdapterErrorCode.NEEDS_AUTH,
                "Douyin login is required",
                retryable=False,
            )
        return {
            "items": ({"id": "favorites", "name": "Favorites"},),
            "next_cursor": None,
            "complete": True,
            "ordering_stable": True,
        }

    async def favorite_page(
        self,
        collection_id: str,
        cursor: str | None,
    ) -> dict[str, object]:
        if collection_id not in {"favorites", "image-favorites"}:
            raise ValueError("unknown Douyin collection")
        self.favorite_responses.pop_all()
        if cursor is None:
            await self.client.navigate(_PROFILE_URL)
            opened = await self.client.page.evaluate(self._OPEN_FAVORITES_SCRIPT, None)
        else:
            if cursor != self._next_favorite_cursor:
                raise AdapterError(
                    AdapterErrorCode.ENUMERATION_INCOMPLETE,
                    "Douyin favorite cursor no longer matches the active page",
                    retryable=True,
                )
            opened = await self.client.page.evaluate(self._LOAD_MORE_SCRIPT, None)
        if opened is not True:
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Douyin favorites tab could not be located",
                retryable=False,
            )
        await asyncio.sleep(self.response_wait_seconds)
        page_payload: dict[str, Any] | None = None
        for capture in self.favorite_responses.pop_all():
            candidate = _find_page(capture.payload)
            if candidate is not None:
                page_payload = candidate
        if page_payload is None:
            raise AdapterError(
                AdapterErrorCode.ENUMERATION_INCOMPLETE,
                "Douyin favorite response was not captured; completeness is unknown",
                retryable=True,
            )
        entries = page_payload.get("aweme_list")
        assert isinstance(entries, list)
        public_entries: list[dict[str, object]] = []
        for raw_entry in entries:
            entry = _mapping(raw_entry, message="Douyin favorite entry changed")
            item_id = str(entry.get("aweme_id") or entry.get("id") or "")
            if not item_id:
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "Douyin favorite has no identity",
                    retryable=False,
                )
            self.detail_cache[item_id] = entry
            kind = "images" if isinstance(entry.get("images"), list) else "video"
            if collection_id == "image-favorites" and kind != "images":
                continue
            public_entries.append(
                {"aweme_id": item_id, "aweme_type": kind, "revision": None}
            )
        has_more = bool(page_payload.get("has_more"))
        next_cursor_value = page_payload.get("max_cursor") or page_payload.get("cursor")
        next_cursor = str(next_cursor_value) if has_more and next_cursor_value is not None else None
        if has_more and next_cursor is None:
            raise AdapterError(
                AdapterErrorCode.ENUMERATION_INCOMPLETE,
                "Douyin response indicates more items without a cursor",
                retryable=True,
            )
        if cursor is not None and cursor == next_cursor:
            raise AdapterError(
                AdapterErrorCode.ENUMERATION_INCOMPLETE,
                "Douyin repeated the favorite cursor",
                retryable=True,
            )
        self._next_favorite_cursor = next_cursor
        return {
            "items": public_entries,
            "next_cursor": next_cursor,
            "complete": not has_more,
            "ordering_stable": True,
            "total_count": None,
        }

    async def item_detail(self, item_id: str) -> dict[str, object]:
        cached = self.detail_cache.get(item_id)
        if cached is not None:
            return cached
        self.detail_responses.pop_all()
        await self.client.navigate(f"https://www.douyin.com/video/{item_id}")
        await asyncio.sleep(0.75)
        for capture in self.detail_responses.pop_all():
            detail = _find_detail(capture.payload)
            if detail is not None and str(detail.get("aweme_id") or detail.get("id")) == item_id:
                self.detail_cache[item_id] = detail
                return detail
        raise AdapterError(
            AdapterErrorCode.MEDIA_UNAVAILABLE,
            "Douyin detail response was not captured; enumerate again",
            retryable=True,
        )


def _fingerprint(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _parse_page(payload: dict[str, object], *, context: str) -> tuple[
    list[dict[str, Any]], str | None, bool, bool, int | None
]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, (list, tuple)):
        raise AdapterError(
            AdapterErrorCode.LAYOUT_CHANGED,
            f"Douyin {context} response has no item list",
            retryable=False,
        )
    items = [_mapping(item, message=f"Douyin {context} entry changed") for item in raw_items]
    next_cursor = payload.get("next_cursor")
    complete = payload.get("complete")
    stable = payload.get("ordering_stable")
    total = payload.get("total_count")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise AdapterError(AdapterErrorCode.LAYOUT_CHANGED, "Invalid cursor type", retryable=False)
    if not isinstance(complete, bool) or not isinstance(stable, bool):
        raise AdapterError(
            AdapterErrorCode.LAYOUT_CHANGED,
            f"Douyin {context} completeness evidence is missing",
            retryable=False,
        )
    if total is not None and not isinstance(total, int):
        raise AdapterError(
            AdapterErrorCode.LAYOUT_CHANGED,
            "Douyin total count changed type",
            retryable=False,
        )
    return items, next_cursor, complete, stable, total


def _http_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, str) and item.startswith(("http://", "https://"))
    ]


def _video_url(detail: dict[str, Any]) -> str | None:
    video = detail.get("video")
    if not isinstance(video, dict):
        return None
    play_address = video.get("play_addr") or video.get("playAddr")
    if isinstance(play_address, dict):
        urls = _http_list(play_address.get("url_list") or play_address.get("urlList"))
        if urls:
            return urls[0]
    bit_rates = video.get("bit_rate") or video.get("bitRate")
    if isinstance(bit_rates, list):
        for bit_rate in bit_rates:
            if isinstance(bit_rate, dict):
                address = bit_rate.get("play_addr") or bit_rate.get("playAddr")
                if isinstance(address, dict):
                    urls = _http_list(address.get("url_list") or address.get("urlList"))
                    if urls:
                        return urls[0]
    return None


def _image_url(image: dict[str, Any]) -> tuple[str | None, str]:
    display = image.get("display_image") or image.get("displayImage")
    if isinstance(display, dict):
        urls = _http_list(display.get("url_list") or display.get("urlList"))
        if urls:
            return urls[0], "page-high"
    urls = _http_list(image.get("url_list") or image.get("urlList"))
    return (urls[0], "fallback") if urls else (None, "fallback")


def _availability(value: object) -> SourceAvailability:
    if value in {"unavailable", "deleted"}:
        return SourceAvailability.UNAVAILABLE
    if value in {"restricted", "private"}:
        return SourceAvailability.RESTRICTED
    return SourceAvailability.AVAILABLE


def _native_track(detail: dict[str, Any], item_id: str) -> tuple[TextSegment, ...]:
    raw_track = detail.get("text_track")
    if not isinstance(raw_track, list):
        return ()
    segments: list[TextSegment] = []
    for index, raw_segment in enumerate(raw_track):
        segment = _mapping(raw_segment, message="Douyin text track changed")
        start = segment.get("start")
        end = segment.get("end")
        text = segment.get("text")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Douyin text track timestamp changed",
                retryable=False,
            )
        if not isinstance(text, str) or not text.strip():
            continue
        segments.append(
            TextSegment(
                segment_id=f"{item_id}:subtitle:{index}",
                start_time=float(start),
                end_time=float(end),
                text=text.strip(),
                raw_text=text,
                source=TextSource.NATIVE_SUBTITLE,
                provenance=("douyin:text-track",),
            )
        )
    return tuple(segments)


def _subtitle_candidate_count(detail: dict[str, Any]) -> int:
    count = 0
    video = detail.get("video")
    if isinstance(video, dict):
        cla_info = video.get("cla_info")
        if isinstance(cla_info, dict) and isinstance(cla_info.get("caption_infos"), list):
            count += len(cla_info["caption_infos"])
        if isinstance(video.get("subtitleInfos"), list):
            count += len(video["subtitleInfos"])
    for key in ("auto_captions", "auto_video_caption_info"):
        if isinstance(detail.get(key), (dict, list)):
            count += 1
    return count


class DouyinAdapter(BaseAdapter):
    platform = Platform.DOUYIN

    def __init__(
        self,
        *,
        bridge: DouyinBridge,
        asset_store: AssetStoreLike | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.bridge = bridge
        self.asset_store = asset_store
        self.now = now or (lambda: datetime.now(UTC))

    async def check_session(self) -> SessionStatus:
        return await self.bridge.check_session()

    async def begin_login(self) -> LoginInstruction:
        return LoginInstruction(
            action=LoginAction.SCAN_QR,
            message="Complete Douyin's QR or device confirmation in the dedicated browser.",
            checkpoint="douyin-login",
        )

    async def list_collections(self, cursor: str | None = None) -> CursorPage[Collection]:
        payload = await self.bridge.collection_page(cursor)
        items, next_cursor, complete, stable, total = _parse_page(
            payload, context="collection"
        )
        collections: list[Collection] = []
        for entry in items:
            identifier = str(entry.get("id") or "")
            name = str(entry.get("name") or "")
            if not identifier or not name:
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "Douyin collection identity changed",
                    retryable=False,
                )
            collections.append(
                Collection(
                    platform=self.platform,
                    platform_collection_id=identifier,
                    name=name,
                    source_url=f"https://www.douyin.com/user/self?showTab={identifier}",
                    adapter_version=ADAPTER_VERSION,
                )
            )
        return CursorPage(
            items=tuple(collections),
            next_cursor=next_cursor,
            complete=complete,
            ordering_stable=stable,
            total_count=total,
        )

    async def list_favorites(
        self,
        collection: Collection,
        cursor: str | None = None,
    ) -> CursorPage[FavoriteRef]:
        if collection.platform != self.platform:
            raise ValueError("Douyin adapter received a cross-platform collection")
        payload = await self.bridge.favorite_page(collection.platform_collection_id, cursor)
        items, next_cursor, complete, stable, total = _parse_page(
            payload, context="favorite"
        )
        references: list[FavoriteRef] = []
        for entry in items:
            item_id = str(entry.get("aweme_id") or entry.get("id") or "")
            if not item_id:
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "Douyin favorite has no canonical identity",
                    retryable=False,
                )
            revision = str(entry.get("revision") or "") or None
            is_images = entry.get("aweme_type") == "images"
            path = "note" if is_images else "video"
            references.append(
                FavoriteRef(
                    canonical_id=f"douyin:{item_id}",
                    platform=self.platform,
                    platform_item_id=item_id,
                    source_url=f"https://www.douyin.com/{path}/{item_id}",
                    source_revision=revision,
                    metadata_fingerprint=_fingerprint(
                        {"id": item_id, "type": entry.get("aweme_type"), "revision": revision}
                    ),
                )
            )
        return CursorPage(
            items=tuple(references),
            next_cursor=next_cursor,
            complete=complete,
            ordering_stable=stable,
            total_count=total,
        )

    async def fetch_item(self, reference: FavoriteRef) -> NormalizedItem:
        if reference.platform != self.platform:
            raise ValueError("Douyin adapter received a cross-platform reference")
        detail = await self.bridge.item_detail(reference.platform_item_id)
        detail_id = str(detail.get("aweme_id") or detail.get("id") or "")
        if detail_id != reference.platform_item_id:
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Douyin detail identity changed",
                retryable=False,
            )
        raw_images = detail.get("images") or []
        if not isinstance(raw_images, list):
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Douyin image list changed type",
                retryable=False,
            )
        image_assets: list[Asset] = []
        image_dimensions: dict[str, tuple[int, int]] = {}
        downgrades: list[str] = []
        for ordinal, raw_image in enumerate(raw_images):
            image = _mapping(raw_image, message="Douyin image entry changed")
            url, quality = _image_url(image)
            width = image.get("width")
            height = image.get("height")
            if url is None:
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "Douyin image exposes no usable URL",
                    retryable=True,
                )
            if not isinstance(width, int) or not isinstance(height, int) or min(width, height) < 1:
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "Douyin image dimensions are missing",
                    retryable=False,
                )
            asset_id = f"{reference.platform_item_id}:image:{ordinal}"
            image_assets.append(
                Asset(
                    asset_id=asset_id,
                    ordinal=ordinal,
                    kind=AssetKind.IMAGE,
                    source_url=url,
                    quality=quality,
                )
            )
            image_dimensions[asset_id] = (width, height)
            if quality == "fallback":
                downgrades.append(asset_id)

        assets = list(image_assets)
        if not image_assets:
            video_url = _video_url(detail)
            if video_url is None:
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "Douyin video exposes no usable URL",
                    retryable=True,
                )
            assets.append(
                Asset(
                    asset_id=f"{reference.platform_item_id}:video",
                    ordinal=0,
                    kind=AssetKind.VIDEO,
                    source_url=video_url,
                    quality="page-exposed",
                )
            )

        raw_description = detail.get("desc")
        description: str | None = raw_description if isinstance(raw_description, str) else None
        blocks: list[ContentBlock] = []
        if description:
            blocks.append(
                ContentBlock(ordinal=0, kind=ContentBlockKind.TEXT, text=description)
            )
        for asset in image_assets:
            blocks.append(
                ContentBlock(
                    ordinal=len(blocks),
                    kind=ContentBlockKind.ASSET,
                    asset_id=asset.asset_id,
                )
            )
        if len(image_assets) > 1:
            content_type = ContentType.GALLERY
        elif image_assets:
            content_type = ContentType.IMAGE_POST
        else:
            content_type = ContentType.VIDEO
        native_subtitles = _native_track(detail, reference.platform_item_id)
        candidate_count = _subtitle_candidate_count(detail)
        subtitle_probe = (
            "usable" if native_subtitles else "candidate_exposed" if candidate_count else "not_exposed"
        )
        author_data = detail.get("author")
        author = author_data.get("nickname") if isinstance(author_data, dict) else None
        observed_at = self.now()
        return NormalizedItem(
            canonical_id=reference.canonical_id,
            platform=self.platform,
            content_type=content_type,
            source_url=reference.source_url,
            title=(description or "Unavailable Douyin item")[:120],
            author=str(author or "Unknown author"),
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            source_availability=_availability(detail.get("availability")),
            original_text=description or None,
            native_subtitles=native_subtitles,
            assets=tuple(assets),
            content_blocks=tuple(blocks),
            source_revision=reference.source_revision,
            metadata_fingerprint=reference.metadata_fingerprint
            or _fingerprint(
                {
                    "id": detail_id,
                    "type": detail.get("aweme_type"),
                    "description": description,
                    "updated": detail.get("create_time"),
                }
            ),
            platform_metadata={
                "image_dimensions": image_dimensions,
                "image_quality_downgrades": downgrades,
                "subtitle_probe": subtitle_probe,
                "subtitle_candidate_count": candidate_count,
                "requires_local_asr": content_type == ContentType.VIDEO
                and not native_subtitles,
                "requires_adaptive_frame_ocr": content_type == ContentType.VIDEO,
                "requires_timeline_fusion": content_type == ContentType.VIDEO,
                "ordered_inline_ocr": bool(image_assets),
                "extraction": "authenticated-page-response",
            },
            adapter_version=ADAPTER_VERSION,
        )

    def fuse_video_text(
        self,
        spoken: tuple[TextSegment, ...],
        visual: tuple[TextSegment, ...],
    ) -> FusionResult:
        return fuse_timelines((*spoken, *visual))

    async def download_assets(
        self,
        item: NormalizedItem,
        temp_dir: Path,
    ) -> tuple[Asset, ...]:
        del temp_dir
        if self.asset_store is None:
            raise AdapterError(
                AdapterErrorCode.MEDIA_UNAVAILABLE,
                "Douyin asset storage is not configured",
                retryable=True,
            )
        raw_dimensions = item.platform_metadata.get("image_dimensions")
        dimensions = raw_dimensions if isinstance(raw_dimensions, dict) else {}
        downloaded: list[Asset] = []
        for asset in item.assets:
            if asset.source_url is None:
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "Douyin asset URL is missing",
                    retryable=True,
                )
            suffix = Path(urlsplit(asset.source_url).path).suffix.lower()
            extension = suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".mp4"} else (
                ".mp4" if asset.kind == AssetKind.VIDEO else ".jpg"
            )
            stored = await asyncio.to_thread(
                self.asset_store.download_static,
                asset.source_url,
                canonical_id=item.canonical_id,
                asset_id=asset.asset_id,
                title=item.title,
                extension=extension,
                ordinal=asset.ordinal,
            )
            if not stored.path.is_file() or stored.path.is_symlink():
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "Douyin asset storage returned an unsafe file",
                    retryable=True,
                )
            if asset.kind == AssetKind.IMAGE:
                try:
                    with Image.open(stored.path) as image:
                        actual_dimensions = image.size
                        image.verify()
                except (OSError, UnidentifiedImageError) as error:
                    stored.path.unlink(missing_ok=True)
                    raise AdapterError(
                        AdapterErrorCode.MEDIA_UNAVAILABLE,
                        "Douyin image failed local decoding",
                        retryable=True,
                    ) from error
                expected = dimensions.get(asset.asset_id)
                expected_tuple = tuple(expected) if isinstance(expected, (list, tuple)) else None
                if expected_tuple is not None and actual_dimensions != expected_tuple:
                    stored.path.unlink(missing_ok=True)
                    raise AdapterError(
                        AdapterErrorCode.MEDIA_UNAVAILABLE,
                        "Douyin image dimensions do not match page metadata",
                        retryable=True,
                    )
            downloaded.append(
                asset.model_copy(
                    update={
                        "local_path": stored.path,
                        "sha256": stored.sha256,
                        "size_bytes": stored.size_bytes,
                        "mime_type": stored.mime_type,
                    }
                )
            )
        return tuple(downloaded)

    async def diagnose(self, error: Exception) -> AdapterDiagnostic:
        code = (
            f"douyin.{error.code.value}"
            if isinstance(error, AdapterError)
            else "douyin.unexpected"
        )
        return AdapterDiagnostic(
            code=code,
            summary="Douyin adapter operation failed; inspect the sanitized code.",
            context={"exception_type": type(error).__name__},
        )
