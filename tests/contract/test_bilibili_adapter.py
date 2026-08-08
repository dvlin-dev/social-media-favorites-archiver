import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError, ExtractorError

import social_media_favorites_archiver.adapters.bilibili as bilibili_module
from social_media_favorites_archiver.adapters.base import (
    AdapterError,
    AdapterErrorCode,
    FavoriteRef,
    SessionState,
    SessionStatus,
)
from social_media_favorites_archiver.adapters.bilibili import (
    BilibiliAdapter,
    BilibiliCollectionDiscovery,
    BilibiliPageDiscovery,
    YtDlpBridge,
    YtDlpLogSink,
)
from social_media_favorites_archiver.browser.interception import PageFetchResult
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


class UnavailableBridge(FixtureBridge):
    def extract(self, url: str, *, flat: bool = False):
        del url, flat
        raise AdapterError(
            AdapterErrorCode.MEDIA_UNAVAILABLE,
            "source item is unavailable",
            retryable=False,
        )


class SessionClient:
    def __init__(self) -> None:
        self.navigations: list[str] = []

    async def navigate(self, url: str) -> None:
        self.navigations.append(url)

    async def fetch_json(self, url: str) -> PageFetchResult:
        assert url == "https://api.bilibili.com/x/web-interface/nav"
        return PageFetchResult(
            status=200,
            content_type="application/json",
            payload={"code": 0, "data": {"isLogin": True, "mid": 123}},
        )


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


def test_page_discovery_navigates_to_bilibili_origin_before_session_fetch() -> None:
    async def exercise() -> None:
        client = SessionClient()
        discovery = BilibiliPageDiscovery(client)

        status = await discovery.check_session()

        assert status.authenticated
        assert client.navigations == ["https://www.bilibili.com/"]

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


def test_expected_extraction_failure_is_preserved_as_unavailable_metadata() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    adapter = BilibiliAdapter(
        bridge=UnavailableBridge(fixture),
        discovery=FixtureDiscovery(fixture),
        now=lambda: NOW,
    )
    reference = FavoriteRef(
        canonical_id="bilibili:BV1deleted",
        platform=Platform.BILIBILI,
        platform_item_id="BV1deleted",
        source_url="https://www.bilibili.com/video/BV1deleted",
        metadata_fingerprint="sha256:" + "0" * 64,
    )

    item = asyncio.run(adapter.fetch_item(reference))

    assert item.source_availability == SourceAvailability.UNAVAILABLE
    assert item.title == "Unavailable Bilibili item"
    assert item.assets == ()
    assert item.platform_metadata["requires_local_asr"] is False


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
    assert options["sleep_interval_requests"] == 1.0
    assert options["retries"] == 2
    assert options["extractor_retries"] == 2
    assert "fake-private-cookie" not in "\n".join(sink.messages)


def test_yt_dlp_reuses_one_authenticated_cookie_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.params = dict(options)
            self.extract_flat_values: list[bool] = []
            instances.append(self)

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            del url, download
            self.extract_flat_values.append(bool(self.params["extract_flat"]))
            return {"id": "fixture"}

    monkeypatch.setattr(bilibili_module, "YoutubeDL", FakeYoutubeDL)
    bridge = YtDlpBridge(browser_profile=tmp_path / "dedicated-profile")

    bridge.extract("https://www.bilibili.com/video/BV1fixture", flat=True)
    bridge.extract("https://www.bilibili.com/video/BV1fixture", flat=False)

    assert len(instances) == 1
    assert instances[0].extract_flat_values == [True, False]


def test_yt_dlp_maps_expected_extractor_error_to_media_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.params = dict(options)

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            del url, download
            try:
                raise ExtractorError("fixture unavailable", expected=True)
            except ExtractorError:
                raise DownloadError("fixture unavailable", sys.exc_info()) from None

    monkeypatch.setattr(bilibili_module, "YoutubeDL", FakeYoutubeDL)
    bridge = YtDlpBridge(browser_profile=tmp_path / "dedicated-profile")

    with pytest.raises(AdapterError) as captured:
        bridge.extract("https://www.bilibili.com/video/BV1fixture")

    assert captured.value.code == AdapterErrorCode.MEDIA_UNAVAILABLE
    assert captured.value.retryable is False


def test_yt_dlp_maps_missing_bvid_extractor_field_to_media_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.params = dict(options)

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            del url, download
            try:
                raise KeyError("bvid")
            except KeyError:
                raise DownloadError("fixture unavailable", sys.exc_info()) from None

    monkeypatch.setattr(bilibili_module, "YoutubeDL", FakeYoutubeDL)
    bridge = YtDlpBridge(browser_profile=tmp_path / "dedicated-profile")

    with pytest.raises(AdapterError) as captured:
        bridge.extract("https://www.bilibili.com/video/av123")

    assert captured.value.code == AdapterErrorCode.MEDIA_UNAVAILABLE
    assert captured.value.retryable is False


def test_yt_dlp_backs_off_and_retries_rate_limit_without_exposing_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.params = dict(options)

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            nonlocal attempts
            del url, download
            attempts += 1
            if attempts < 3:
                try:
                    raise ExtractorError("HTTP Error 412: fixture rate limit")
                except ExtractorError:
                    raise DownloadError("fixture rate limit", sys.exc_info()) from None
            return {"id": "fixture"}

    monkeypatch.setattr(bilibili_module, "YoutubeDL", FakeYoutubeDL)
    bridge = YtDlpBridge(
        browser_profile=tmp_path / "dedicated-profile",
        sleep=delays.append,
        jitter=lambda _start, _end: 0,
        rate_limit_backoff_seconds=2,
    )

    result = bridge.extract("https://www.bilibili.com/video/BV1fixture")

    assert result == {"id": "fixture"}
    assert attempts == 3
    assert delays == [2, 4]


def test_adapter_diagnostic_does_not_include_exception_message() -> None:
    diagnostic = asyncio.run(
        _adapter().diagnose(RuntimeError("Cookie: session=fake-private-cookie"))
    )

    assert "fake-private-cookie" not in diagnostic.model_dump_json()
    assert diagnostic.context["exception_type"] == "RuntimeError"
