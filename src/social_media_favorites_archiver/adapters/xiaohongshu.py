"""Xiaohongshu favorites through an authenticated browser page context."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit

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
)
from social_media_favorites_archiver.storage.assets import StoredAsset

ADAPTER_VERSION = "xiaohongshu-v1"
_HOME_URL = "https://www.xiaohongshu.com/explore"


class XiaohongshuBridge(Protocol):
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


class XiaohongshuBrowserBridge:
    """Drive the site's own UI/state and keep ephemeral access data in memory."""

    _SESSION_SCRIPT = """
    async () => {
      const store = window.__INITIAL_STATE__?.user;
      const loggedIn = Boolean(store?.loggedIn?._value);
      const userId = store?.userInfo?._value?.userId || null;
      return {
        authenticated: loggedIn && Boolean(userId),
        profile_path: loggedIn && userId ? `/user/profile/${userId}` : null,
      };
    }
    """
    _FAVORITES_SCRIPT = """
    async ({offset, pageSize}) => {
      const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      const leaf = [...document.querySelectorAll('div,span')].find((node) =>
        node.children.length === 0 && node.textContent.trim() === '收藏'
      );
      if (leaf && window.__INITIAL_STATE__?.user?.activeTab?._value?.label !== '收藏') {
        leaf.click();
        for (let attempt = 0; attempt < 20; attempt += 1) {
          if (window.__INITIAL_STATE__?.user?.activeTab?._value?.label === '收藏') break;
          await sleep(100);
        }
      }
      const read = () => {
        const store = window.__INITIAL_STATE__?.user;
        const tabIndex = store?.activeTab?._value?.index;
        const items = store?.notes?._value?.[tabIndex] || [];
        const query = store?.noteQueries?._value?.[tabIndex] || {};
        return {items, query};
      };
      let current = read();
      for (let batch = 0; batch < 20 && offset >= current.items.length && current.query.hasMore; batch += 1) {
        const previousLength = current.items.length;
        window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'});
        for (let attempt = 0; attempt < 50; attempt += 1) {
          await sleep(100);
          current = read();
          if (current.items.length > previousLength || !current.query.hasMore) break;
        }
        if (current.items.length === previousLength) break;
      }
      const selected = current.items.slice(offset, offset + pageSize).map((entry) => {
        const card = entry?.noteCard || entry || {};
        return {
          id: card?.noteId || entry?.id,
          type: card?.type || 'normal',
          revision: null,
          access_token: card?.xsecToken || entry?.xsecToken || null,
          card: {
            title: card?.displayTitle || card?.title || '',
            desc: card?.desc || '',
            type: card?.type || 'normal',
            user: {nickname: card?.user?.nickname || ''},
          },
        };
      }).filter((entry) => Boolean(entry.id));
      const nextOffset = offset + selected.length;
      const hasMore = Boolean(current.query.hasMore) || nextOffset < current.items.length;
      return {
        items: selected,
        next_cursor: hasMore ? String(nextOffset) : null,
        complete: !hasMore,
        ordering_stable: true,
        total_count: null,
      };
    }
    """
    _DETAIL_SCRIPT = """
    async ({itemId}) => {
      const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      for (let attempt = 0; attempt < 50; attempt += 1) {
        const map = window.__INITIAL_STATE__?.note?.noteDetailMap || {};
        const wrapper = Object.values(map).find((candidate) => candidate?.note?.noteId === itemId);
        if (wrapper?.note) return wrapper.note;
        await sleep(100);
      }
      return null;
    }
    """

    def __init__(
        self,
        client: PageContextClient,
        *,
        page_size: int = 20,
        session_poll_attempts: int = 25,
        session_poll_interval: float = 0.2,
    ) -> None:
        if page_size < 1:
            raise ValueError("Xiaohongshu page size must be positive")
        if session_poll_attempts < 1:
            raise ValueError("Xiaohongshu session poll attempts must be positive")
        if session_poll_interval < 0:
            raise ValueError("Xiaohongshu session poll interval must not be negative")
        self.client = client
        self.page_size = page_size
        self.session_poll_attempts = session_poll_attempts
        self.session_poll_interval = session_poll_interval
        self.profile_path: str | None = None
        self.access_tokens: dict[str, str] = {}
        self.card_cache: dict[str, dict[str, object]] = {}
        self.favorite_responses = ResponseInterceptor(
            url_substring="/api/sns/web/v1/user_posted"
        )
        self.detail_responses = ResponseInterceptor(url_substring="/api/sns/web/v1/feed")
        self.favorite_responses.attach(client.page)
        self.detail_responses.attach(client.page)

    async def check_session(self) -> SessionStatus:
        try:
            await self.client.navigate(_HOME_URL)
            payload: dict[str, Any] = {}
            for attempt in range(self.session_poll_attempts):
                result = await self.client.page.evaluate(self._SESSION_SCRIPT, None)
                payload = _mapping(result, message="Xiaohongshu session state changed")
                if payload.get("authenticated") and isinstance(
                    payload.get("profile_path"), str
                ):
                    break
                if attempt + 1 < self.session_poll_attempts:
                    await asyncio.sleep(self.session_poll_interval)
        except AdapterError:
            raise
        except Exception:
            return SessionStatus(
                state=SessionState.NEEDS_LOGIN,
                diagnostic_code="xiaohongshu.session_unreachable",
            )
        if not payload.get("authenticated") or not isinstance(payload.get("profile_path"), str):
            return SessionStatus(
                state=SessionState.EXPIRED,
                diagnostic_code="xiaohongshu.session_expired",
            )
        self.profile_path = str(payload["profile_path"])
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def collection_page(self, cursor: str | None) -> dict[str, object]:
        if cursor is not None:
            raise AdapterError(
                AdapterErrorCode.ENUMERATION_INCOMPLETE,
                "Xiaohongshu collection cursor is no longer valid",
                retryable=True,
            )
        status = await self.check_session()
        if not status.authenticated:
            raise AdapterError(
                AdapterErrorCode.NEEDS_AUTH,
                "Xiaohongshu login is required",
                retryable=False,
            )
        return {
            "items": ({"id": "saved", "name": "Saved notes"},),
            "next_cursor": None,
            "complete": True,
            "ordering_stable": True,
        }

    async def _open_profile(self) -> None:
        if self.profile_path is None:
            status = await self.check_session()
            if not status.authenticated:
                raise AdapterError(
                    AdapterErrorCode.NEEDS_AUTH,
                    "Xiaohongshu login is required",
                    retryable=False,
                )
        assert self.profile_path is not None
        await self.client.navigate(f"https://www.xiaohongshu.com{self.profile_path}")

    async def favorite_page(
        self,
        collection_id: str,
        cursor: str | None,
    ) -> dict[str, object]:
        if collection_id != "saved":
            raise ValueError("unknown Xiaohongshu collection")
        await self._open_profile()
        try:
            offset = int(cursor or "0")
        except ValueError as error:
            raise AdapterError(
                AdapterErrorCode.ENUMERATION_INCOMPLETE,
                "Xiaohongshu favorite cursor is invalid",
                retryable=True,
            ) from error
        result = await self.client.page.evaluate(
            self._FAVORITES_SCRIPT,
            {"offset": offset, "pageSize": self.page_size},
        )
        payload = _mapping(result, message="Xiaohongshu favorite layout changed")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Xiaohongshu favorite state has no item list",
                retryable=False,
            )
        public_items: list[dict[str, object]] = []
        for raw_item in raw_items:
            entry = _mapping(raw_item, message="Xiaohongshu favorite entry changed")
            item_id = str(entry.get("id") or "")
            token = entry.pop("access_token", None)
            card = entry.pop("card", None)
            if item_id and isinstance(token, str) and token:
                self.access_tokens[item_id] = token
            if item_id and isinstance(card, dict):
                self.card_cache[item_id] = cast(dict[str, object], card)
            public_items.append(entry)
        payload["items"] = public_items
        return payload

    async def item_detail(self, item_id: str) -> dict[str, object]:
        token = self.access_tokens.get(item_id)
        if token is None:
            return self._unavailable_card_detail(item_id)
        query = urlencode({"xsec_token": token, "xsec_source": "pc_user"})
        await self.client.navigate(f"https://www.xiaohongshu.com/explore/{item_id}?{query}")
        result = await self.client.page.evaluate(self._DETAIL_SCRIPT, {"itemId": item_id})
        if result is None:
            return self._unavailable_card_detail(item_id)
        return _mapping(result, message="Xiaohongshu detail layout changed")

    def _unavailable_card_detail(self, item_id: str) -> dict[str, object]:
        card = self.card_cache.get(item_id, {})
        user = card.get("user")
        return {
            "noteId": item_id,
            "title": str(card.get("title") or "Unavailable Xiaohongshu item"),
            "desc": str(card.get("desc") or "") or None,
            "type": str(card.get("type") or "normal"),
            "user": user if isinstance(user, dict) else {},
            "availability": "unavailable",
            "imageList": [],
        }


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
            f"Xiaohongshu {context} response has no item list",
            retryable=False,
        )
    items = [_mapping(entry, message=f"Xiaohongshu {context} entry changed") for entry in raw_items]
    next_cursor = payload.get("next_cursor")
    complete = payload.get("complete")
    ordering_stable = payload.get("ordering_stable")
    total_count = payload.get("total_count")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise AdapterError(AdapterErrorCode.LAYOUT_CHANGED, "Invalid cursor type", retryable=False)
    if not isinstance(complete, bool) or not isinstance(ordering_stable, bool):
        raise AdapterError(
            AdapterErrorCode.LAYOUT_CHANGED,
            f"Xiaohongshu {context} completeness evidence is missing",
            retryable=False,
        )
    if total_count is not None and not isinstance(total_count, int):
        raise AdapterError(
            AdapterErrorCode.LAYOUT_CHANGED,
            "Xiaohongshu total count changed type",
            retryable=False,
        )
    return items, next_cursor, complete, ordering_stable, total_count


def _availability(value: object) -> SourceAvailability:
    if value in {"unavailable", "deleted"}:
        return SourceAvailability.UNAVAILABLE
    if value in {"restricted", "private"}:
        return SourceAvailability.RESTRICTED
    return SourceAvailability.AVAILABLE


def _http_url(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _image_url(image: dict[str, Any]) -> tuple[str | None, str]:
    default = _http_url(image.get("urlDefault"))
    if default is not None:
        return default, "page-default"
    direct = _http_url(image.get("url"))
    if direct is not None:
        return direct, "fallback"
    info_list = image.get("infoList")
    if isinstance(info_list, list):
        for raw_info in info_list:
            if isinstance(raw_info, dict):
                candidate = _http_url(raw_info.get("url"))
                if candidate is not None:
                    return candidate, "fallback"
    return _http_url(image.get("urlPre")), "fallback"


def _dimensions_match(
    actual: tuple[int, int],
    expected: tuple[int, int],
    *,
    quality: str,
) -> bool:
    """Accept page renditions when their aspect ratio preserves source geometry."""
    del quality
    if actual == expected:
        return True
    actual_width, actual_height = actual
    expected_width, expected_height = expected
    if min(actual_width, actual_height, expected_width, expected_height) < 1:
        return False
    aspect_error = abs(
        (actual_width / actual_height) / (expected_width / expected_height) - 1
    )
    return aspect_error <= 0.02


def _video_url(detail: dict[str, Any]) -> str | None:
    video = detail.get("video") or detail.get("videoInfo")
    if not isinstance(video, dict):
        return None
    media = video.get("media")
    if isinstance(media, dict):
        stream = media.get("stream")
        if isinstance(stream, dict):
            candidates: list[dict[str, Any]] = []
            for variants in stream.values():
                if isinstance(variants, list):
                    for variant in variants:
                        if isinstance(variant, dict):
                            candidates.append(variant)
            candidates.sort(
                key=lambda variant: (
                    int(variant.get("defaultStream") == 1),
                    variant.get("weight") if isinstance(variant.get("weight"), int) else 0,
                ),
                reverse=True,
            )
            for variant in candidates:
                candidate = _http_url(variant.get("masterUrl"))
                if candidate is not None:
                    return candidate
                backups = variant.get("backupUrls")
                if isinstance(backups, list):
                    for backup in backups:
                        candidate = _http_url(backup)
                        if candidate is not None:
                            return candidate
    return _http_url(video.get("masterUrl") or video.get("url"))


class XiaohongshuAdapter(BaseAdapter):
    platform = Platform.XIAOHONGSHU

    def __init__(
        self,
        *,
        bridge: XiaohongshuBridge,
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
            message="Complete Xiaohongshu's QR or device confirmation in the dedicated browser.",
            checkpoint="xiaohongshu-login",
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
                    "Xiaohongshu collection identity changed",
                    retryable=False,
                )
            collections.append(
                Collection(
                    platform=self.platform,
                    platform_collection_id=identifier,
                    name=name,
                    source_url=f"https://www.xiaohongshu.com/user/profile/me?tab={identifier}",
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
            raise ValueError("Xiaohongshu adapter received a cross-platform collection")
        payload = await self.bridge.favorite_page(collection.platform_collection_id, cursor)
        items, next_cursor, complete, stable, total = _parse_page(
            payload, context="favorite"
        )
        references: list[FavoriteRef] = []
        for entry in items:
            item_id = str(entry.get("id") or "")
            if not item_id:
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "Xiaohongshu favorite entry has no canonical identity",
                    retryable=False,
                )
            revision = str(entry.get("revision") or "") or None
            references.append(
                FavoriteRef(
                    canonical_id=f"xiaohongshu:{item_id}",
                    platform=self.platform,
                    platform_item_id=item_id,
                    source_url=f"https://www.xiaohongshu.com/explore/{item_id}",
                    source_revision=revision,
                    metadata_fingerprint=_fingerprint(
                        {"id": item_id, "type": entry.get("type"), "revision": revision}
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
            raise ValueError("Xiaohongshu adapter received a cross-platform reference")
        detail = await self.bridge.item_detail(reference.platform_item_id)
        detail_id = str(detail.get("noteId") or detail.get("id") or "")
        if detail_id != reference.platform_item_id:
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Xiaohongshu detail identity changed",
                retryable=False,
            )
        raw_images = detail.get("imageList") or []
        if not isinstance(raw_images, list):
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Xiaohongshu image list changed type",
                retryable=False,
            )
        image_assets: list[Asset] = []
        dimensions: dict[str, tuple[int, int]] = {}
        downgrades: list[str] = []
        for ordinal, raw_image in enumerate(raw_images):
            image = _mapping(raw_image, message="Xiaohongshu image entry changed")
            url, quality = _image_url(image)
            if url is None:
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "Xiaohongshu image exposes no usable static URL",
                    retryable=True,
                )
            width = image.get("width")
            height = image.get("height")
            if not isinstance(width, int) or not isinstance(height, int) or min(width, height) < 1:
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "Xiaohongshu image dimensions are missing",
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
            dimensions[asset_id] = (width, height)
            if quality != "original":
                downgrades.append(asset_id)

        video_url = _video_url(detail)
        assets = list(image_assets)
        if str(detail.get("type") or "") == "video":
            if video_url is None:
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "Xiaohongshu video exposes no usable media URL",
                    retryable=True,
                )
            assets.append(
                Asset(
                    asset_id=f"{reference.platform_item_id}:video",
                    ordinal=len(assets),
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
        if str(detail.get("type") or "") == "video":
            content_type = ContentType.VIDEO
        elif len(image_assets) > 1:
            content_type = ContentType.GALLERY
        elif image_assets:
            content_type = ContentType.IMAGE_POST
        else:
            content_type = ContentType.ARTICLE
        user = detail.get("user")
        author = user.get("nickname") if isinstance(user, dict) else None
        observed_at = self.now()
        return NormalizedItem(
            canonical_id=reference.canonical_id,
            platform=self.platform,
            content_type=content_type,
            source_url=reference.source_url,
            title=str(detail.get("title") or "Unavailable Xiaohongshu item"),
            author=str(author or "Unknown author"),
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            source_availability=_availability(detail.get("availability")),
            original_text=description,
            assets=tuple(assets),
            content_blocks=tuple(blocks),
            source_revision=reference.source_revision,
            metadata_fingerprint=reference.metadata_fingerprint
            or _fingerprint(
                {
                    "id": detail_id,
                    "type": detail.get("type"),
                    "title": detail.get("title"),
                    "updated": detail.get("lastUpdateTime"),
                }
            ),
            platform_metadata={
                "image_dimensions": dimensions,
                "image_quality_downgrades": downgrades,
                "subtitle_probe": "not_exposed",
                "requires_local_asr": content_type == ContentType.VIDEO,
                "requires_adaptive_frame_ocr": content_type == ContentType.VIDEO,
                "ordered_inline_ocr": bool(image_assets),
                "extraction": "authenticated-page-context",
            },
            adapter_version=ADAPTER_VERSION,
        )

    async def download_assets(
        self,
        item: NormalizedItem,
        temp_dir: Path,
    ) -> tuple[Asset, ...]:
        del temp_dir
        if self.asset_store is None:
            raise AdapterError(
                AdapterErrorCode.MEDIA_UNAVAILABLE,
                "Xiaohongshu asset storage is not configured",
                retryable=True,
            )
        raw_dimensions = item.platform_metadata.get("image_dimensions")
        dimensions = raw_dimensions if isinstance(raw_dimensions, dict) else {}
        downloaded: list[Asset] = []
        for asset in item.assets:
            if asset.source_url is None:
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "Xiaohongshu asset URL is missing",
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
                    "Xiaohongshu asset storage returned an unsafe file",
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
                        "Xiaohongshu image failed local decoding",
                        retryable=True,
                    ) from error
                expected = dimensions.get(asset.asset_id)
                expected_tuple = tuple(expected) if isinstance(expected, (list, tuple)) else None
                if expected_tuple is not None and not _dimensions_match(
                    actual_dimensions,
                    expected_tuple,
                    quality=asset.quality or "fallback",
                ):
                    stored.path.unlink(missing_ok=True)
                    raise AdapterError(
                        AdapterErrorCode.MEDIA_UNAVAILABLE,
                        "Xiaohongshu image dimensions do not match page metadata",
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
            f"xiaohongshu.{error.code.value}"
            if isinstance(error, AdapterError)
            else "xiaohongshu.unexpected"
        )
        return AdapterDiagnostic(
            code=code,
            summary="Xiaohongshu adapter operation failed; inspect the sanitized code.",
            context={"exception_type": type(error).__name__},
        )
