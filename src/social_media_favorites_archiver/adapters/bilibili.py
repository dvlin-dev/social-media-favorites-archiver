"""Bilibili favorites adapter backed by authenticated pages and yt-dlp."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast

from yt_dlp import YoutubeDL  # type: ignore[import-untyped]
from yt_dlp.utils import DownloadError  # type: ignore[import-untyped]

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
from social_media_favorites_archiver.browser.interception import PageContextClient
from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    Collection,
    ContentType,
    NormalizedItem,
    Platform,
    SourceAvailability,
    TextSegment,
)
from social_media_favorites_archiver.processors.subtitles import normalize_yt_dlp_subtitles
from social_media_favorites_archiver.safety.paths import resolve_within
from social_media_favorites_archiver.safety.redaction import redact_text

ADAPTER_VERSION = "bilibili-v1"


class YtDlpClient(Protocol):
    def extract(self, url: str, *, flat: bool = False) -> dict[str, Any]: ...

    def download(self, url: str, output_dir: Path) -> tuple[Path, ...]: ...


class BilibiliCollectionDiscovery(Protocol):
    async def check_session(self) -> SessionStatus: ...

    async def collections(self) -> tuple[Collection, ...]: ...


class YtDlpLogSink:
    """yt-dlp logger that stores only redacted messages and is silent by default."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def _append(self, message: str) -> None:
        self.messages.append(redact_text(message))

    def debug(self, message: str) -> None:
        if not message.startswith("[debug]"):
            self._append(message)

    def info(self, message: str) -> None:
        self._append(message)

    def warning(self, message: str) -> None:
        self._append(message)

    def error(self, message: str) -> None:
        self._append(message)


class YtDlpBridge:
    """Direct wrapper around yt-dlp's Bilibili extractors and browser cookies."""

    def __init__(
        self,
        *,
        browser_profile: str | Path,
        browser_name: str = "chrome",
        logger: YtDlpLogSink | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        rate_limit_retries: int = 2,
        rate_limit_backoff_seconds: float = 30,
    ) -> None:
        if rate_limit_retries < 0:
            raise ValueError("Bilibili rate-limit retries must not be negative")
        if rate_limit_backoff_seconds < 0:
            raise ValueError("Bilibili rate-limit backoff must not be negative")
        self.browser_profile = Path(browser_profile)
        self.browser_name = browser_name
        self.logger = logger or YtDlpLogSink()
        self._sleep = sleep
        self._jitter = jitter
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_backoff_seconds = rate_limit_backoff_seconds
        self._instance: YoutubeDL | None = None
        self._lock = Lock()

    @property
    def base_options(self) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": False,
            "logger": self.logger,
            "cookiesfrombrowser": (
                self.browser_name,
                str(self.browser_profile),
                None,
                None,
            ),
            "skip_download": True,
            "listsubtitles": True,
            "cachedir": False,
            "sleep_interval_requests": 1.0,
            "retries": 2,
            "extractor_retries": 2,
        }

    def extract(self, url: str, *, flat: bool = False) -> dict[str, Any]:
        with self._lock:
            downloader = self._downloader()
            downloader.params.update(
                {
                    **self.base_options,
                    "extract_flat": flat,
                    "skip_download": True,
                    "listsubtitles": True,
                }
            )
            for attempt in range(self.rate_limit_retries + 1):
                try:
                    result = downloader.extract_info(url, download=False)
                    break
                except DownloadError as error:
                    cause = error.exc_info[1] if error.exc_info else None
                    structurally_unavailable = (
                        isinstance(cause, KeyError)
                        and bool(cause.args)
                        and cause.args[0] in {"aid", "bvid"}
                    )
                    if bool(getattr(cause, "expected", False)) or structurally_unavailable:
                        raise AdapterError(
                            AdapterErrorCode.MEDIA_UNAVAILABLE,
                            "Bilibili source item is unavailable",
                            retryable=False,
                        ) from error
                    cause_text = str(cause).lower()
                    rate_limited = any(
                        marker in cause_text
                        for marker in ("412", "429", "too many requests", "rate limit")
                    )
                    if rate_limited and attempt < self.rate_limit_retries:
                        base = self.rate_limit_backoff_seconds * (2**attempt)
                        self._sleep(base + self._jitter(0, max(1.0, base * 0.25)))
                        continue
                    raise AdapterError(
                        (
                            AdapterErrorCode.RATE_LIMITED
                            if rate_limited
                            else AdapterErrorCode.MEDIA_UNAVAILABLE
                        ),
                        (
                            "Bilibili extraction is rate limited"
                            if rate_limited
                            else "Bilibili extraction failed"
                        ),
                        retryable=True,
                    ) from error
        if not isinstance(result, dict):
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "yt-dlp returned no structured Bilibili result",
                retryable=False,
            )
        return cast(dict[str, Any], result)

    def download(self, url: str, output_dir: Path) -> tuple[Path, ...]:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        before = {path.resolve() for path in output_dir.rglob("*") if path.is_file()}
        with self._lock:
            downloader = self._downloader()
            downloader.params.update(
                {
                    **self.base_options,
                    "extract_flat": False,
                    "skip_download": False,
                    "listsubtitles": False,
                    "paths": {"home": str(output_dir)},
                    "outtmpl": {"default": "%(id).80B.%(ext)s"},
                    "restrictfilenames": True,
                }
            )
            downloader.extract_info(url, download=True)
        downloaded = tuple(
            sorted(
                path.resolve()
                for path in output_dir.rglob("*")
                if path.is_file() and path.resolve() not in before and not path.name.endswith(".part")
            )
        )
        return downloaded

    def _downloader(self) -> YoutubeDL:
        if self._instance is None:
            self._instance = YoutubeDL({**self.base_options, "extract_flat": False})
        return self._instance


class BilibiliPageDiscovery:
    """Discover the authorized user's collection IDs inside the page context."""

    def __init__(self, client: PageContextClient) -> None:
        self.client = client
        self._mid: str | None = None

    async def check_session(self) -> SessionStatus:
        try:
            await self.client.navigate("https://www.bilibili.com/")
            result = await self.client.fetch_json(
                "https://api.bilibili.com/x/web-interface/nav"
            )
        except Exception:
            return SessionStatus(
                state=SessionState.NEEDS_LOGIN,
                diagnostic_code="bilibili.session_unreachable",
            )
        data = result.payload.get("data")
        if result.payload.get("code") != 0 or not isinstance(data, dict) or not data.get("isLogin"):
            return SessionStatus(
                state=SessionState.EXPIRED,
                diagnostic_code="bilibili.session_expired",
            )
        self._mid = str(data.get("mid"))
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def collections(self) -> tuple[Collection, ...]:
        status = await self.check_session()
        if not status.authenticated or self._mid is None:
            raise AdapterError(
                AdapterErrorCode.NEEDS_AUTH,
                "Bilibili login is required",
                retryable=False,
            )
        result = await self.client.fetch_json(
            "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
            f"?up_mid={self._mid}"
        )
        data = result.payload.get("data")
        entries = data.get("list") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "Bilibili collection response is missing its list",
                retryable=False,
            )
        collections: list[Collection] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("id") is None or not entry.get("title"):
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "Bilibili collection response has an invalid entry",
                    retryable=False,
                )
            collection_id = str(entry["id"])
            collections.append(
                Collection(
                    platform=Platform.BILIBILI,
                    platform_collection_id=collection_id,
                    name=str(entry["title"]),
                    source_url=f"https://www.bilibili.com/medialist/detail/ml{collection_id}",
                    adapter_version=ADAPTER_VERSION,
                )
            )
        return tuple(collections)


def _fingerprint(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _platform_item_id(entry: dict[str, Any]) -> str:
    candidate = str(entry.get("bvid") or entry.get("id") or "")
    if "_p" in candidate:
        candidate = candidate.split("_p", 1)[0]
    if candidate.startswith("BV") or (candidate.startswith("av") and candidate[2:].isdigit()):
        return candidate
    aid = entry.get("aid")
    if isinstance(aid, int) or (isinstance(aid, str) and aid.isdigit()):
        return f"av{aid}"
    raise AdapterError(
        AdapterErrorCode.LAYOUT_CHANGED,
        "Bilibili entry has no canonical BV/AV identity",
        retryable=False,
    )


def _availability(value: object) -> SourceAvailability:
    if value in {"private", "subscriber_only", "premium_only", "needs_auth"}:
        return SourceAvailability.RESTRICTED
    if value in {"unavailable", "deleted"}:
        return SourceAvailability.UNAVAILABLE
    return SourceAvailability.AVAILABLE


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class BilibiliAdapter(BaseAdapter):
    platform = Platform.BILIBILI

    def __init__(
        self,
        *,
        bridge: YtDlpClient,
        discovery: BilibiliCollectionDiscovery,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.bridge = bridge
        self.discovery = discovery
        self.now = now or (lambda: datetime.now(UTC))

    async def check_session(self) -> SessionStatus:
        return await self.discovery.check_session()

    async def begin_login(self) -> LoginInstruction:
        return LoginInstruction(
            action=LoginAction.SCAN_QR,
            message="Complete Bilibili's QR or device confirmation in the dedicated Chrome profile.",
            checkpoint="bilibili-login",
        )

    async def list_collections(self, cursor: str | None = None) -> CursorPage[Collection]:
        if cursor is not None:
            raise ValueError("Bilibili collection discovery is returned as one complete page")
        return CursorPage(
            items=await self.discovery.collections(),
            complete=True,
            ordering_stable=True,
        )

    async def list_favorites(
        self,
        collection: Collection,
        cursor: str | None = None,
    ) -> CursorPage[FavoriteRef]:
        if cursor is not None:
            raise ValueError("yt-dlp Bilibili favorite extraction returns one complete page")
        if collection.platform != self.platform or collection.source_url is None:
            raise ValueError("Bilibili collection requires a Bilibili source URL")
        info = await asyncio.to_thread(self.bridge.extract, collection.source_url, flat=True)
        raw_entries = info.get("entries")
        if not isinstance(raw_entries, list):
            raise AdapterError(
                AdapterErrorCode.LAYOUT_CHANGED,
                "yt-dlp favorite result is missing entries",
                retryable=False,
            )
        references: list[FavoriteRef] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise AdapterError(
                    AdapterErrorCode.LAYOUT_CHANGED,
                    "yt-dlp favorite result contains an invalid entry",
                    retryable=False,
                )
            item_id = _platform_item_id(entry)
            source_url = str(
                entry.get("webpage_url")
                or entry.get("url")
                or f"https://www.bilibili.com/video/{item_id}"
            )
            references.append(
                FavoriteRef(
                    canonical_id=f"bilibili:{item_id}",
                    platform=self.platform,
                    platform_item_id=item_id,
                    source_url=source_url,
                    source_revision=str(entry.get("modified_timestamp") or entry.get("timestamp") or "")
                    or None,
                    metadata_fingerprint=_fingerprint(
                        {
                            "id": item_id,
                            "title": entry.get("title"),
                            "timestamp": entry.get("timestamp"),
                            "availability": entry.get("availability"),
                        }
                    ),
                )
            )
        return CursorPage(items=tuple(references), complete=True, ordering_stable=True)

    async def fetch_item(self, reference: FavoriteRef) -> NormalizedItem:
        if reference.platform != self.platform:
            raise ValueError("Bilibili adapter received a cross-platform reference")
        try:
            info = await asyncio.to_thread(self.bridge.extract, reference.source_url, flat=False)
        except AdapterError as error:
            if error.code != AdapterErrorCode.MEDIA_UNAVAILABLE or error.retryable:
                raise
            return NormalizedItem(
                canonical_id=reference.canonical_id,
                platform=self.platform,
                content_type=ContentType.VIDEO,
                source_url=reference.source_url,
                title="Unavailable Bilibili item",
                author="Unknown author",
                first_seen_at=self.now(),
                last_seen_at=self.now(),
                source_availability=SourceAvailability.UNAVAILABLE,
                source_revision=reference.source_revision,
                metadata_fingerprint=reference.metadata_fingerprint
                or _fingerprint({"id": reference.platform_item_id, "availability": "unavailable"}),
                platform_metadata={
                    "parts": [],
                    "subtitle_available": False,
                    "requires_local_asr": False,
                    "extractor": "yt-dlp:BiliBili",
                    "availability_probe": "expected_extractor_error",
                },
                adapter_version=ADAPTER_VERSION,
            )
        raw_parts = info.get("entries") if isinstance(info.get("entries"), list) else []
        parts: list[dict[str, object]] = []
        subtitles: list[TextSegment] = []
        offset = 0.0
        if raw_parts:
            for position, part in enumerate(raw_parts, start=1):
                if not isinstance(part, dict):
                    raise AdapterError(
                        AdapterErrorCode.LAYOUT_CHANGED,
                        "Bilibili multi-part result contains an invalid part",
                        retryable=False,
                    )
                part_id = str(part.get("id") or f"part-{position}")
                duration_value = part.get("duration")
                duration = float(duration_value) if isinstance(duration_value, (int, float)) else 0.0
                parts.append(
                    {
                        "id": part_id,
                        "position": position,
                        "title": str(part.get("title") or f"Part {position}"),
                        "duration": duration,
                    }
                )
                subtitles.extend(
                    normalize_yt_dlp_subtitles(
                        part.get("subtitles"),
                        time_offset=offset,
                        segment_prefix=part_id,
                    )
                )
                offset += duration
        else:
            subtitles.extend(
                normalize_yt_dlp_subtitles(
                    info.get("subtitles"),
                    segment_prefix=reference.platform_item_id,
                )
            )
        cover_url = info.get("thumbnail")
        assets: tuple[Asset, ...] = ()
        if isinstance(cover_url, str) and cover_url.startswith(("http://", "https://")):
            assets = (
                Asset(
                    asset_id=f"{reference.platform_item_id}-cover",
                    ordinal=0,
                    kind=AssetKind.COVER,
                    source_url=cover_url,
                ),
            )
        availability = _availability(info.get("availability"))
        metadata_fingerprint = reference.metadata_fingerprint or _fingerprint(
            {
                "id": reference.platform_item_id,
                "title": info.get("title"),
                "timestamp": info.get("timestamp"),
                "availability": info.get("availability"),
            }
        )
        return NormalizedItem(
            canonical_id=reference.canonical_id,
            platform=self.platform,
            content_type=ContentType.VIDEO,
            source_url=reference.source_url,
            title=str(info.get("title") or "Unavailable Bilibili item"),
            author=str(info.get("uploader") or "Unknown author"),
            author_url=info.get("uploader_url") if isinstance(info.get("uploader_url"), str) else None,
            published_at=_timestamp(info.get("timestamp")),
            first_seen_at=self.now(),
            last_seen_at=self.now(),
            source_availability=availability,
            original_text=info.get("description") if isinstance(info.get("description"), str) else None,
            native_subtitles=tuple(subtitles),
            assets=assets,
            source_revision=reference.source_revision,
            metadata_fingerprint=metadata_fingerprint,
            platform_metadata={
                "parts": parts,
                "subtitle_available": bool(subtitles),
                "requires_local_asr": not bool(subtitles),
                "extractor": "yt-dlp:BiliBili",
            },
            adapter_version=ADAPTER_VERSION,
        )

    async def download_assets(
        self,
        item: NormalizedItem,
        temp_dir: Path,
    ) -> tuple[Asset, ...]:
        paths = await asyncio.to_thread(self.bridge.download, item.source_url, temp_dir)
        assets: list[Asset] = []
        for ordinal, downloaded in enumerate(paths):
            path = resolve_within(temp_dir, downloaded)
            if not path.is_file() or path.is_symlink():
                raise AdapterError(
                    AdapterErrorCode.MEDIA_UNAVAILABLE,
                    "yt-dlp produced an unsafe or missing file",
                    retryable=True,
                )
            kind = AssetKind.AUDIO if path.suffix.lower() in {".m4a", ".mp3", ".wav"} else AssetKind.VIDEO
            assets.append(
                Asset(
                    asset_id=f"{item.canonical_id}:download:{ordinal}",
                    ordinal=ordinal,
                    kind=kind,
                    local_path=path,
                    sha256=_file_sha256(path),
                    size_bytes=path.stat().st_size,
                )
            )
        return tuple(assets)

    async def diagnose(self, error: Exception) -> AdapterDiagnostic:
        if isinstance(error, AdapterError):
            code = f"bilibili.{error.code.value}"
        else:
            code = "bilibili.unexpected"
        return AdapterDiagnostic(
            code=code,
            summary="Bilibili adapter operation failed; inspect the sanitized diagnostic code.",
            context={"exception_type": type(error).__name__},
        )
