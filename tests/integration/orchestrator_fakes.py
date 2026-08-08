from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

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
from social_media_favorites_archiver.models import (
    Asset,
    Collection,
    ContentType,
    NormalizedItem,
    Platform,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def collection(identifier: str = "fixture") -> Collection:
    return Collection(
        platform=Platform.BILIBILI,
        platform_collection_id=identifier,
        name=f"Collection {identifier}",
        source_url=f"https://space.bilibili.com/1/favlist?fid={identifier}",
        adapter_version="fixture-v1",
    )


def item(identifier: str, *, observed_at: datetime = NOW) -> NormalizedItem:
    return NormalizedItem(
        canonical_id=f"bilibili:{identifier}",
        platform=Platform.BILIBILI,
        content_type=ContentType.VIDEO,
        source_url=f"https://www.bilibili.com/video/{identifier}",
        title=f"Video {identifier}",
        author="Fixture author",
        first_seen_at=NOW,
        last_seen_at=observed_at,
        source_revision=f"revision-{identifier}",
        metadata_fingerprint=fingerprint(identifier),
        adapter_version="fixture-v1",
    )


def reference(identifier: str) -> FavoriteRef:
    return FavoriteRef(
        canonical_id=f"bilibili:{identifier}",
        platform=Platform.BILIBILI,
        platform_item_id=identifier,
        source_url=f"https://www.bilibili.com/video/{identifier}",
        source_revision=f"revision-{identifier}",
        metadata_fingerprint=fingerprint(identifier),
    )


class FixtureAdapter(BaseAdapter):
    platform = Platform.BILIBILI

    def __init__(
        self,
        identifiers: tuple[str, ...],
        *,
        page_size: int = 2,
        fail_at_offset: int | None = None,
        error_code: AdapterErrorCode = AdapterErrorCode.ENUMERATION_INCOMPLETE,
        ordering_stable: bool = True,
        expose_total: bool = True,
    ) -> None:
        self.identifiers = identifiers
        self.page_size = page_size
        self.fail_at_offset = fail_at_offset
        self.error_code = error_code
        self.ordering_stable = ordering_stable
        self.expose_total = expose_total
        self.fetch_calls: list[str] = []
        self.favorite_page_calls = 0

    async def check_session(self) -> SessionStatus:
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def begin_login(self) -> LoginInstruction:
        return LoginInstruction(action=LoginAction.OPEN_BROWSER, message="fixture")

    async def list_collections(self, cursor: str | None = None) -> CursorPage[Collection]:
        return CursorPage(items=(collection(),), complete=True, ordering_stable=True)

    async def list_favorites(
        self,
        selected_collection: Collection,
        cursor: str | None = None,
    ) -> CursorPage[FavoriteRef]:
        del selected_collection
        offset = int(cursor or "0")
        if self.fail_at_offset == offset:
            raise AdapterError(self.error_code, "sanitized fixture failure", retryable=True)
        self.favorite_page_calls += 1
        identifiers = self.identifiers[offset : offset + self.page_size]
        next_offset = offset + len(identifiers)
        complete = next_offset >= len(self.identifiers)
        return CursorPage(
            items=tuple(reference(identifier) for identifier in identifiers),
            next_cursor=None if complete else str(next_offset),
            complete=complete,
            ordering_stable=self.ordering_stable,
            total_count=len(self.identifiers) if self.expose_total else None,
        )

    async def fetch_item(self, favorite: FavoriteRef) -> NormalizedItem:
        self.fetch_calls.append(favorite.canonical_id)
        return item(favorite.platform_item_id)

    async def download_assets(
        self,
        selected_item: NormalizedItem,
        temp_dir: Path,
    ) -> tuple[Asset, ...]:
        del selected_item, temp_dir
        return ()

    async def diagnose(self, error: Exception) -> AdapterDiagnostic:
        del error
        return AdapterDiagnostic(code="fixture.error", summary="sanitized fixture failure")
