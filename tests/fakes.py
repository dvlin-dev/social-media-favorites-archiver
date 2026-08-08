"""Deterministic, network-free adapters shared by hardening tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from social_media_favorites_archiver.adapters.base import (
    AdapterDiagnostic,
    BaseAdapter,
    CursorPage,
    FavoriteRef,
    LoginAction,
    LoginInstruction,
    SessionState,
    SessionStatus,
)
from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    Collection,
    ContentType,
    NormalizedItem,
    Platform,
    SourceAvailability,
    TextSegment,
    TextSource,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _item(
    platform: Platform,
    identifier: str,
    content_type: ContentType,
    *,
    availability: SourceAvailability = SourceAvailability.AVAILABLE,
    metadata: dict[str, object] | None = None,
    subtitles: tuple[TextSegment, ...] = (),
    assets: tuple[Asset, ...] = (),
) -> NormalizedItem:
    return NormalizedItem(
        canonical_id=f"{platform.value}:{identifier}",
        platform=platform,
        content_type=content_type,
        source_url=f"https://example.invalid/{platform.value}/{identifier}",
        title=f"Synthetic {identifier}",
        author="Fixture author",
        first_seen_at=NOW,
        last_seen_at=NOW,
        source_availability=availability,
        original_text=f"Synthetic source text for {identifier}.",
        native_subtitles=subtitles,
        assets=assets,
        metadata_fingerprint=_fingerprint(f"{platform.value}:{identifier}"),
        platform_metadata=metadata or {},
        adapter_version="deterministic-fake-v1",
    )


def _image(identifier: str, ordinal: int) -> Asset:
    return Asset(
        asset_id=f"{identifier}:image:{ordinal}",
        ordinal=ordinal,
        kind=AssetKind.IMAGE,
        source_url=f"https://example.invalid/{identifier}-{ordinal}.png",
    )


class DeterministicAdapter(BaseAdapter):
    def __init__(self, platform: Platform, items: tuple[NormalizedItem, ...]) -> None:
        self.platform = platform
        self.items = {item.canonical_id: item for item in items}
        self.collection = Collection(
            platform=platform,
            platform_collection_id="fixture",
            name="Synthetic fixture collection",
            source_url=f"https://example.invalid/{platform.value}/collection",
            adapter_version="deterministic-fake-v1",
        )

    async def check_session(self) -> SessionStatus:
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def begin_login(self) -> LoginInstruction:
        return LoginInstruction(action=LoginAction.OPEN_BROWSER, message="Synthetic login.")

    async def list_collections(self, cursor: str | None = None) -> CursorPage[Collection]:
        if cursor is not None:
            raise ValueError("deterministic collection page is complete")
        return CursorPage(items=(self.collection,), complete=True, ordering_stable=True)

    async def list_favorites(
        self,
        collection: Collection,
        cursor: str | None = None,
    ) -> CursorPage[FavoriteRef]:
        if collection != self.collection or cursor is not None:
            raise ValueError("invalid deterministic favorite request")
        references = tuple(
            FavoriteRef(
                canonical_id=item.canonical_id,
                platform=self.platform,
                platform_item_id=item.canonical_id.split(":", 1)[1],
                source_url=item.source_url,
                metadata_fingerprint=item.metadata_fingerprint,
            )
            for item in self.items.values()
        )
        return CursorPage(
            items=references,
            complete=True,
            ordering_stable=True,
            total_count=len(references),
        )

    async def fetch_item(self, reference: FavoriteRef) -> NormalizedItem:
        return self.items[reference.canonical_id]

    async def download_assets(
        self,
        item: NormalizedItem,
        temp_dir: Path,
    ) -> tuple[Asset, ...]:
        del temp_dir
        return item.assets

    async def diagnose(self, error: Exception) -> AdapterDiagnostic:
        del error
        return AdapterDiagnostic(code="fixture.error", summary="Synthetic diagnostic.")


def deterministic_adapters() -> tuple[DeterministicAdapter, ...]:
    subtitle = TextSegment(
        segment_id="bili-native-1",
        start_time=0,
        end_time=1,
        text="Synthetic subtitle.",
        source=TextSource.NATIVE_SUBTITLE,
    )
    bilibili = DeterministicAdapter(
        Platform.BILIBILI,
        (
            _item(
                Platform.BILIBILI,
                "native-video",
                ContentType.VIDEO,
                subtitles=(subtitle,),
                metadata={"subtitle_available": True, "requires_local_asr": False},
            ),
            _item(
                Platform.BILIBILI,
                "asr-video",
                ContentType.VIDEO,
                metadata={"subtitle_available": False, "requires_local_asr": True},
            ),
            _item(
                Platform.BILIBILI,
                "multipart-video",
                ContentType.VIDEO,
                metadata={
                    "subtitle_available": True,
                    "requires_local_asr": False,
                    "parts": [
                        {"position": 1, "title": "Part one", "duration": 1.0},
                        {"position": 2, "title": "Part two", "duration": 2.0},
                    ],
                },
            ),
        ),
    )
    xiaohongshu = DeterministicAdapter(
        Platform.XIAOHONGSHU,
        (
            _item(Platform.XIAOHONGSHU, "text", ContentType.ARTICLE),
            _item(
                Platform.XIAOHONGSHU,
                "gallery",
                ContentType.GALLERY,
                assets=(_image("xhs-gallery", 0), _image("xhs-gallery", 1)),
                metadata={"ordered_inline_ocr": True},
            ),
            _item(
                Platform.XIAOHONGSHU,
                "video",
                ContentType.VIDEO,
                metadata={
                    "requires_local_asr": True,
                    "requires_adaptive_frame_ocr": True,
                },
            ),
            _item(
                Platform.XIAOHONGSHU,
                "unavailable",
                ContentType.ARTICLE,
                availability=SourceAvailability.UNAVAILABLE,
            ),
        ),
    )
    douyin = DeterministicAdapter(
        Platform.DOUYIN,
        (
            _item(
                Platform.DOUYIN,
                "burned-caption-video",
                ContentType.VIDEO,
                metadata={
                    "requires_local_asr": True,
                    "requires_adaptive_frame_ocr": True,
                    "requires_timeline_fusion": True,
                },
            ),
            _item(
                Platform.DOUYIN,
                "gallery",
                ContentType.GALLERY,
                assets=(_image("douyin-gallery", 0), _image("douyin-gallery", 1)),
                metadata={"ordered_inline_ocr": True},
            ),
            _item(
                Platform.DOUYIN,
                "unavailable",
                ContentType.VIDEO,
                availability=SourceAvailability.UNAVAILABLE,
            ),
        ),
    )
    return bilibili, xiaohongshu, douyin
