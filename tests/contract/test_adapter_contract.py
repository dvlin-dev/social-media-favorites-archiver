import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    validate_cursor_progression,
)
from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    Collection,
    ContentType,
    NormalizedItem,
    Platform,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SHA = f"sha256:{'a' * 64}"


class ContractAdapter(BaseAdapter):
    platform = Platform.BILIBILI

    async def check_session(self) -> SessionStatus:
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def begin_login(self) -> LoginInstruction:
        return LoginInstruction(
            action=LoginAction.SCAN_QR,
            message="Complete the platform QR flow in the dedicated browser.",
        )

    async def list_collections(self, cursor: str | None = None) -> CursorPage[Collection]:
        if cursor is None:
            return CursorPage(
                items=(
                    Collection(
                        platform=self.platform,
                        platform_collection_id="collection-1",
                        name="Fixture collection",
                    ),
                ),
                next_cursor="collections-2",
                complete=False,
                ordering_stable=True,
            )
        return CursorPage(items=(), complete=True, ordering_stable=True)

    async def list_favorites(
        self,
        collection: Collection,
        cursor: str | None = None,
    ) -> CursorPage[FavoriteRef]:
        return CursorPage(
            items=(
                FavoriteRef(
                    canonical_id="bilibili:BV1example",
                    platform=self.platform,
                    platform_item_id="BV1example",
                    source_url="https://www.bilibili.com/video/BV1example",
                ),
            ),
            complete=True,
            ordering_stable=True,
        )

    async def fetch_item(self, reference: FavoriteRef) -> NormalizedItem:
        return NormalizedItem(
            canonical_id=reference.canonical_id,
            platform=self.platform,
            content_type=ContentType.VIDEO,
            source_url=reference.source_url,
            title="Fixture video",
            author="Fixture author",
            first_seen_at=NOW,
            last_seen_at=NOW,
            assets=(
                Asset(asset_id="cover", ordinal=0, kind=AssetKind.COVER),
                Asset(asset_id="video", ordinal=1, kind=AssetKind.VIDEO),
            ),
            metadata_fingerprint=SHA,
            adapter_version="contract-v1",
        )

    async def download_assets(
        self,
        item: NormalizedItem,
        temp_dir: Path,
    ) -> tuple[Asset, ...]:
        return item.assets

    async def diagnose(self, error: Exception) -> AdapterDiagnostic:
        return AdapterDiagnostic(
            code="fixture.error",
            summary="Fixture diagnostic",
            context={
                "authorization": "Bearer fake-private-token",
                "attempt": 2,
            },
        )


def test_adapter_contract_cursor_identity_ordering_and_completeness(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = ContractAdapter()
        assert (await adapter.check_session()).state == SessionState.AUTHENTICATED
        first = await adapter.list_collections()
        second = await adapter.list_collections(first.next_cursor)
        validate_cursor_progression(None, first)
        validate_cursor_progression(first.next_cursor, second)
        assert first.complete is False
        assert second.complete is True

        favorites = await adapter.list_favorites(first.items[0])
        item = await adapter.fetch_item(favorites.items[0])
        assets = await adapter.download_assets(item, tmp_path)
        assert item.canonical_id == favorites.items[0].canonical_id
        assert [asset.ordinal for asset in assets] == [0, 1]

    asyncio.run(exercise())


def test_contract_models_session_expiry_user_action_and_retry_semantics() -> None:
    expired = SessionStatus(
        state=SessionState.EXPIRED,
        diagnostic_code="session.expired",
    )
    instruction = LoginInstruction(
        action=LoginAction.DEVICE_CONFIRMATION,
        message="Confirm the device in the platform UI.",
        checkpoint="safe-resume-token",
    )
    error = AdapterError(
        AdapterErrorCode.RATE_LIMITED,
        "The platform requested backoff.",
        retryable=True,
    )

    assert expired.authenticated is False
    assert instruction.requires_user_action is True
    assert error.retryable is True


def test_contract_rejects_repeated_or_inconsistent_cursors() -> None:
    repeated = CursorPage[FavoriteRef](
        items=(),
        next_cursor="cursor-1",
        complete=False,
        ordering_stable=True,
    )
    with pytest.raises(ValueError):
        validate_cursor_progression("cursor-1", repeated)
    with pytest.raises(ValueError):
        CursorPage[FavoriteRef](
            items=(),
            next_cursor="unexpected",
            complete=True,
            ordering_stable=True,
        )


def test_adapter_diagnostics_are_sanitized() -> None:
    diagnostic = asyncio.run(ContractAdapter().diagnose(RuntimeError("fake")))
    serialized = diagnostic.model_dump_json()

    assert "fake-private-token" not in serialized
    assert diagnostic.context["authorization"] == "[REDACTED]"

