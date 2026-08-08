"""Connection to an explicitly configured dedicated Chrome CDP session."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from playwright.async_api import Playwright, async_playwright


class BrowserSessionError(RuntimeError):
    """Raised when a safe dedicated browser session cannot be used."""


class PageLike(Protocol):
    @property
    def url(self) -> str: ...

    async def goto(self, url: str, *, wait_until: str) -> Any: ...

    async def evaluate(self, expression: str, arg: object | None = None) -> Any: ...

    def on(self, event: str, handler: object) -> None: ...


class ContextLike(Protocol):
    @property
    def pages(self) -> Sequence[PageLike]: ...

    async def new_page(self) -> PageLike: ...


class BrowserLike(Protocol):
    @property
    def contexts(self) -> Sequence[ContextLike]: ...


class CDPConnector(Protocol):
    async def connect(self, endpoint: str) -> BrowserLike: ...

    async def stop(self) -> None: ...


class _PlaywrightConnector:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None

    async def connect(self, endpoint: str) -> BrowserLike:
        self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.connect_over_cdp(endpoint)
        return cast(BrowserLike, browser)

    async def stop(self) -> None:
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


def validate_dedicated_profile(profile_path: str | Path) -> Path:
    """Reject known default browser roots; the application never copies them."""
    candidate = Path(profile_path).expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    forbidden = (
        home / "Library/Application Support/Google/Chrome",
        home / "Library/Application Support/Chromium",
        home / ".config/google-chrome",
        home / ".config/chromium",
    )
    if any(candidate == root or candidate.is_relative_to(root) for root in forbidden):
        raise BrowserSessionError("a dedicated application browser profile is required")
    return candidate


def _validate_cdp_url(cdp_url: str) -> str:
    parsed = urlsplit(cdp_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserSessionError("CDP endpoint must be an explicit HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BrowserSessionError("CDP endpoint must not contain credentials or query data")
    return cdp_url.rstrip("/")


class BrowserSession:
    """Attach to Chrome and disconnect automation without closing the user's browser."""

    def __init__(
        self,
        *,
        cdp_url: str,
        profile_path: str | Path,
        connector: CDPConnector | None = None,
    ) -> None:
        self.cdp_url = _validate_cdp_url(cdp_url)
        self.profile_path = validate_dedicated_profile(profile_path)
        self.connector = connector or _PlaywrightConnector()
        self._connected = False
        self._context: ContextLike | None = None

    async def connect(self, *, preferred_host: str | None = None) -> PageLike:
        self.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._connected:
            try:
                browser = await self.connector.connect(self.cdp_url)
            except BaseException:
                await self.connector.stop()
                raise
            self._connected = True
            if not browser.contexts:
                await self.stop()
                raise BrowserSessionError("CDP browser has no authenticated context")
            self._context = browser.contexts[0]
        assert self._context is not None
        if preferred_host is not None:
            for page in self._context.pages:
                if urlsplit(page.url).hostname == preferred_host:
                    return page
            return await self._context.new_page()
        return (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

    async def stop(self) -> None:
        if self._connected:
            await self.connector.stop()
            self._connected = False
            self._context = None
