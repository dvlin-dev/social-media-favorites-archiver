"""Ephemeral structured-response interception and authenticated page fetches."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from social_media_favorites_archiver.safety.redaction import redact_text


class PageProtocol(Protocol):
    async def goto(self, url: str, *, wait_until: str) -> Any: ...

    async def evaluate(self, expression: str, arg: object | None = None) -> Any: ...

    def on(self, event: str, handler: object) -> None: ...


class ResponseProtocol(Protocol):
    url: str
    status: int
    headers: Mapping[str, str]

    async def json(self) -> Any: ...


class PageFetchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: int
    content_type: str
    payload: dict[str, Any]


class PageContextClient:
    """Navigate and call fetch inside the authenticated page JavaScript context."""

    _FETCH_SCRIPT = """
    async ({url, method, body}) => {
      const response = await fetch(url, {
        method,
        credentials: 'include',
        headers: body === null ? {} : {'content-type': 'application/json'},
        body: body === null ? undefined : JSON.stringify(body),
      });
      const contentType = response.headers.get('content-type') || '';
      const payload = await response.json();
      return {status: response.status, content_type: contentType, payload};
    }
    """

    def __init__(self, page: PageProtocol) -> None:
        self.page = page

    async def navigate(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def fetch_json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Mapping[str, object] | None = None,
    ) -> PageFetchResult:
        result = await self.page.evaluate(
            self._FETCH_SCRIPT,
            {"url": url, "method": method, "body": None if body is None else dict(body)},
        )
        return PageFetchResult.model_validate(result)


class CapturedResponse(BaseModel):
    """Private response payload retained only in process memory for adapter normalization."""

    model_config = ConfigDict(frozen=True)

    url: str
    status: int
    payload: dict[str, Any]


class ResponseInterceptor:
    """Capture matching JSON responses without persisting raw payloads in diagnostics."""

    def __init__(self, *, url_substring: str) -> None:
        if not url_substring:
            msg = "response URL substring must be non-empty"
            raise ValueError(msg)
        self.url_substring = url_substring
        self._captures: list[CapturedResponse] = []
        self._observations: list[dict[str, object]] = []

    def attach(self, page: PageProtocol) -> None:
        async def capture(response: ResponseProtocol) -> None:
            await self._capture(response)

        page.on("response", capture)

    async def _capture(self, response: ResponseProtocol) -> None:
        if self.url_substring not in response.url:
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        self._captures.append(
            CapturedResponse(url=response.url, status=response.status, payload=payload)
        )
        self._observations.append(
            {
                "url": redact_text(response.url),
                "status": response.status,
                "top_level_keys": sorted(str(key) for key in payload)[:20],
            }
        )

    def pop_all(self) -> tuple[CapturedResponse, ...]:
        captures = tuple(self._captures)
        self._captures.clear()
        return captures

    def diagnostic_summary(self) -> str:
        return json.dumps(
            {"matched_responses": len(self._observations), "observations": self._observations},
            ensure_ascii=False,
            sort_keys=True,
        )
