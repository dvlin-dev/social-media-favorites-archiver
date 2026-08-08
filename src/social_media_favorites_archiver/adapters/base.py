"""Platform-neutral adapter contract and validated pagination."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from social_media_favorites_archiver.models import Asset, Collection, NormalizedItem, Platform
from social_media_favorites_archiver.safety.redaction import redact_mapping


class SessionState(StrEnum):
    AUTHENTICATED = "authenticated"
    NEEDS_LOGIN = "needs_login"
    EXPIRED = "expired"
    NEEDS_USER_ACTION = "needs_user_action"


class SessionStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: SessionState
    diagnostic_code: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.state == SessionState.AUTHENTICATED


class LoginAction(StrEnum):
    OPEN_BROWSER = "open_browser"
    SCAN_QR = "scan_qr"
    CAPTCHA = "captcha"
    DEVICE_CONFIRMATION = "device_confirmation"


class LoginInstruction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: LoginAction
    message: str = Field(min_length=1)
    checkpoint: str | None = None

    @property
    def requires_user_action(self) -> bool:
        return self.action in {
            LoginAction.SCAN_QR,
            LoginAction.CAPTCHA,
            LoginAction.DEVICE_CONFIRMATION,
        }


class FavoriteRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_id: str = Field(min_length=3)
    platform: Platform
    platform_item_id: str = Field(min_length=1)
    source_url: str
    source_revision: str | None = None
    metadata_fingerprint: str | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = "favorite source URL must use HTTP(S)"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> FavoriteRef:
        if self.canonical_id != f"{self.platform.value}:{self.platform_item_id}":
            msg = "favorite canonical identity does not match platform item ID"
            raise ValueError(msg)
        return self


PageItem = TypeVar("PageItem")


class CursorPage(BaseModel, Generic[PageItem]):
    model_config = ConfigDict(frozen=True)

    items: tuple[PageItem, ...]
    next_cursor: str | None = None
    complete: bool
    ordering_stable: bool
    total_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_completion(self) -> CursorPage[PageItem]:
        if self.complete and self.next_cursor is not None:
            msg = "complete pages must not expose a next cursor"
            raise ValueError(msg)
        if not self.complete and not self.next_cursor:
            msg = "incomplete pages require a next cursor"
            raise ValueError(msg)
        return self


def validate_cursor_progression(previous_cursor: str | None, page: CursorPage[object]) -> None:
    if previous_cursor is not None and page.next_cursor == previous_cursor:
        msg = "adapter repeated a pagination cursor"
        raise ValueError(msg)


class AdapterDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    summary: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("context", mode="before")
    @classmethod
    def sanitize_context(cls, value: object) -> object:
        return redact_mapping(value)


class AdapterErrorCode(StrEnum):
    NEEDS_AUTH = "needs_auth"
    NEEDS_USER_ACTION = "needs_user_action"
    RATE_LIMITED = "rate_limited"
    LAYOUT_CHANGED = "layout_changed"
    ENUMERATION_INCOMPLETE = "enumeration_incomplete"
    MEDIA_UNAVAILABLE = "media_unavailable"


class AdapterError(RuntimeError):
    def __init__(self, code: AdapterErrorCode, summary: str, *, retryable: bool) -> None:
        super().__init__(summary)
        self.code = code
        self.retryable = retryable


class BaseAdapter(ABC):
    """Authenticated collection adapter; private signatures stay in the page context."""

    platform: Platform

    @abstractmethod
    async def check_session(self) -> SessionStatus:
        raise NotImplementedError

    @abstractmethod
    async def begin_login(self) -> LoginInstruction:
        raise NotImplementedError

    @abstractmethod
    async def list_collections(self, cursor: str | None = None) -> CursorPage[Collection]:
        raise NotImplementedError

    @abstractmethod
    async def list_favorites(
        self,
        collection: Collection,
        cursor: str | None = None,
    ) -> CursorPage[FavoriteRef]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_item(self, reference: FavoriteRef) -> NormalizedItem:
        raise NotImplementedError

    @abstractmethod
    async def download_assets(
        self,
        item: NormalizedItem,
        temp_dir: Path,
    ) -> tuple[Asset, ...]:
        raise NotImplementedError

    @abstractmethod
    async def diagnose(self, error: Exception) -> AdapterDiagnostic:
        raise NotImplementedError

