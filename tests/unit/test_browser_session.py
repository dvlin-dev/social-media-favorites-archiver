import asyncio
from pathlib import Path

import pytest

from social_media_favorites_archiver.browser.interception import (
    PageContextClient,
    ResponseInterceptor,
)
from social_media_favorites_archiver.browser.session import (
    BrowserSession,
    BrowserSessionError,
    validate_dedicated_profile,
)


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.handlers = {}
        self.evaluations = []

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.url = url
        self.wait_until = wait_until

    async def evaluate(self, script: str, argument):
        self.evaluations.append((script, argument))
        return {
            "status": 200,
            "content_type": "application/json",
            "payload": {"items": [{"id": "fixture-1"}]},
        }

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def emit_response(self, response) -> None:
        result = self.handlers["response"](response)
        if asyncio.iscoroutine(result):
            await result


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.contexts = [FakeContext(page)]


class FakeConnector:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.endpoint = None

    async def connect(self, endpoint: str):
        self.endpoint = endpoint
        return FakeBrowser(self.page)

    async def stop(self) -> None:
        self.stopped = True


class FakeResponse:
    def __init__(self) -> None:
        self.url = "https://example.invalid/api/favorites?signature=fake-private-signature"
        self.status = 200
        self.headers = {"content-type": "application/json"}

    async def json(self):
        return {"items": [{"id": "fixture-1", "caption": "private in-memory text"}]}


def test_browser_session_connects_only_to_explicit_cdp_and_leaves_browser_running(
    tmp_path: Path,
) -> None:
    page = FakePage()
    connector = FakeConnector(page)
    session = BrowserSession(
        cdp_url="http://127.0.0.1:9222",
        profile_path=tmp_path / "dedicated-profile",
        connector=connector,
    )

    connected_page = asyncio.run(session.connect())
    asyncio.run(session.stop())

    assert connected_page is page
    assert connector.endpoint == "http://127.0.0.1:9222"
    assert connector.stopped is True


def test_default_browser_profiles_are_rejected() -> None:
    with pytest.raises(BrowserSessionError):
        validate_dedicated_profile(Path.home() / "Library/Application Support/Google/Chrome")


def test_page_navigation_and_authenticated_context_fetch() -> None:
    async def exercise() -> None:
        page = FakePage()
        client = PageContextClient(page)
        await client.navigate("http://127.0.0.1:8765/mock")
        result = await client.fetch_json(
            "http://127.0.0.1:8765/api/favorites",
            method="POST",
            body={"cursor": "fixture-cursor"},
        )

        assert page.url.endswith("/mock")
        assert result.status == 200
        assert result.payload["items"][0]["id"] == "fixture-1"
        script, argument = page.evaluations[0]
        assert "credentials: 'include'" in script
        assert argument["body"] == {"cursor": "fixture-cursor"}

    asyncio.run(exercise())


def test_response_interception_keeps_payload_in_memory_and_sanitizes_diagnostics() -> None:
    async def exercise() -> None:
        page = FakePage()
        interceptor = ResponseInterceptor(url_substring="/api/favorites")
        interceptor.attach(page)
        await page.emit_response(FakeResponse())

        captures = interceptor.pop_all()
        assert captures[0].payload["items"][0]["id"] == "fixture-1"
        summary = interceptor.diagnostic_summary()
        assert "fake-private-signature" not in summary
        assert "private in-memory text" not in summary

    asyncio.run(exercise())
