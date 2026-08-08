import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from social_media_favorites_archiver.adapters.base import FavoriteRef, SessionState, SessionStatus
from social_media_favorites_archiver.adapters.bilibili import (
    BilibiliAdapter,
    BilibiliCollectionDiscovery,
    YtDlpBridge,
    YtDlpLogSink,
)
from social_media_favorites_archiver.models import Collection, Platform, SourceAvailability

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sanitized" / "bilibili.json"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FixtureDiscovery(BilibiliCollectionDiscovery):
    def __init__(self, fixture) -> None:
        self.fixture = fixture

    async def check_session(self) -> SessionStatus:
        return SessionStatus(state=SessionState.AUTHENTICATED)

    async def collections(self) -> tuple[Collection, ...]:
        return tuple(
            Collection(
                platform=Platform.BILIBILI,
                platform_collection_id=entry["id"],
                name=entry["title"],
                source_url=f"https://www.bilibili.com/medialist/detail/ml{entry['id']}",
                adapter_version="fixture-v1",
            )
            for entry in self.fixture["collections"]
        )


class FixtureBridge:
    def __init__(self, fixture) -> None:
        self.fixture = fixture

    def extract(self, url: str, *, flat: bool = False):
        if "medialist" in url:
            return self.fixture["playlist"]
        item_id = url.rstrip("/").rsplit("/", 1)[-1]
        return self.fixture["items"][item_id]

    def download(self, url: str, output_dir: Path) -> tuple[Path, ...]:
        return ()


def _adapter() -> BilibiliAdapter:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return BilibiliAdapter(
        bridge=FixtureBridge(fixture),
        discovery=FixtureDiscovery(fixture),
        now=lambda: NOW,
    )


def test_collection_discovery_favorite_enumeration_and_canonical_identity() -> None:
    async def exercise() -> None:
        adapter = _adapter()
        assert (await adapter.check_session()).authenticated
        collections = await adapter.list_collections()
        assert collections.complete is True
        assert collections.items[0].platform_collection_id == "fav-001"

        favorites = await adapter.list_favorites(collections.items[0])
        assert favorites.complete is True
        assert [reference.canonical_id for reference in favorites.items] == [
            "bilibili:BV1fixture",
            "bilibili:av123456",
            "bilibili:BV1private",
        ]

    asyncio.run(exercise())


def test_private_item_is_preserved_as_restricted_metadata() -> None:
    reference = FavoriteRef(
        canonical_id="bilibili:BV1private",
        platform=Platform.BILIBILI,
        platform_item_id="BV1private",
        source_url="https://www.bilibili.com/video/BV1private",
    )

    item = asyncio.run(_adapter().fetch_item(reference))

    assert item.source_availability == SourceAvailability.RESTRICTED
    assert item.platform_metadata["requires_local_asr"] is True


def test_yt_dlp_cookie_options_and_diagnostics_are_redacted(tmp_path: Path) -> None:
    sink = YtDlpLogSink()
    bridge = YtDlpBridge(browser_profile=tmp_path / "dedicated-profile", logger=sink)
    options = bridge.base_options
    sink.warning("Cookie: session=fake-private-cookie")

    assert options["cookiesfrombrowser"] == (
        "chrome",
        str(tmp_path / "dedicated-profile"),
        None,
        None,
    )
    assert "fake-private-cookie" not in "\n".join(sink.messages)


def test_adapter_diagnostic_does_not_include_exception_message() -> None:
    diagnostic = asyncio.run(
        _adapter().diagnose(RuntimeError("Cookie: session=fake-private-cookie"))
    )

    assert "fake-private-cookie" not in diagnostic.model_dump_json()
    assert diagnostic.context["exception_type"] == "RuntimeError"
