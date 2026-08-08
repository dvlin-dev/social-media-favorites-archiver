"""Validated domain models shared by adapters, processors, and storage."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class DomainModel(BaseModel):
    """Immutable, strict base for persisted domain values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Platform(StrEnum):
    BILIBILI = "bilibili"
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"


class ContentType(StrEnum):
    VIDEO = "video"
    ARTICLE = "article"
    IMAGE_POST = "image_post"
    GALLERY = "gallery"


class SourceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DELETED = "deleted"
    RESTRICTED = "restricted"


class AssetKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    COVER = "cover"
    KEYFRAME = "keyframe"
    SUBTITLE = "subtitle"


class ContentBlockKind(StrEnum):
    TEXT = "text"
    ASSET = "asset"


class TextSource(StrEnum):
    NATIVE_SUBTITLE = "native_subtitle"
    ASR = "asr"
    OCR = "ocr"
    BURNED_CAPTION = "burned_caption"
    VISUAL_ANNOTATION = "visual_annotation"


class ExtractionType(StrEnum):
    NATIVE_SUBTITLE = "native_subtitle"
    ASR = "asr"
    OCR = "ocr"
    FUSION = "fusion"


class MembershipState(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


def _validate_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = "source URL must use HTTP(S) and include a host"
        raise ValueError(msg)
    if parsed.username or parsed.password:
        msg = "source URL must not contain embedded credentials"
        raise ValueError(msg)
    return value


def _validate_aware_datetime(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        msg = "source timestamps must include timezone information"
        raise ValueError(msg)
    return value


def make_canonical_id(platform: Platform, platform_item_id: str) -> str:
    """Construct a stable identity without accepting ambiguous separators."""
    normalized = platform_item_id.strip()
    if not normalized or ":" in normalized:
        msg = "platform item ID must be non-empty and must not contain ':'"
        raise ValueError(msg)
    return f"{platform.value}:{normalized}"


class Collection(DomainModel):
    platform: Platform
    platform_collection_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source_url: str | None = None
    adapter_version: str | None = None

    @field_validator("platform_collection_id")
    @classmethod
    def validate_platform_collection_id(cls, value: str) -> str:
        if ":" in value:
            msg = "platform collection ID must not contain ':'"
            raise ValueError(msg)
        return value

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)

    @property
    def canonical_id(self) -> str:
        return f"{self.platform.value}:collection:{self.platform_collection_id}"


class Asset(DomainModel):
    asset_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    kind: AssetKind
    source_url: str | None = None
    local_path: Path | None = None
    sha256: Sha256 | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    quality: str | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)


class ContentBlock(DomainModel):
    ordinal: int = Field(ge=0)
    kind: ContentBlockKind
    text: str | None = None
    asset_id: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ContentBlock:
        if self.kind == ContentBlockKind.TEXT and not self.text:
            msg = "text content blocks require text"
            raise ValueError(msg)
        if self.kind == ContentBlockKind.ASSET and not self.asset_id:
            msg = "asset content blocks require an asset_id"
            raise ValueError(msg)
        return self


class TextSegment(DomainModel):
    segment_id: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    text: str = Field(min_length=1)
    raw_text: str | None = None
    source: TextSource
    confidence: float | None = Field(default=None, ge=0, le=1)
    asset_id: str | None = None
    provenance: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_timeline(self) -> TextSegment:
        if self.end_time < self.start_time:
            msg = "segment end_time must not precede start_time"
            raise ValueError(msg)
        return self

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time


_SENSITIVE_METADATA_FRAGMENTS = (
    "authorization",
    "cookie",
    "local_storage",
    "raw_response",
    "request_headers",
    "signed_url",
    "token",
)


def _reject_sensitive_metadata(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if any(fragment in normalized_key for fragment in _SENSITIVE_METADATA_FRAGMENTS):
                msg = "platform metadata contains a forbidden sensitive field"
                raise ValueError(msg)
            _reject_sensitive_metadata(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive_metadata(nested)


class NormalizedItem(DomainModel):
    canonical_id: str = Field(min_length=3)
    platform: Platform
    content_type: ContentType
    source_url: str
    title: str = Field(min_length=1)
    author: str = Field(min_length=1)
    author_url: str | None = None
    published_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    source_availability: SourceAvailability = SourceAvailability.AVAILABLE
    collection_canonical_ids: tuple[str, ...] = ()
    original_text: str | None = None
    native_subtitles: tuple[TextSegment, ...] = ()
    assets: tuple[Asset, ...] = ()
    content_blocks: tuple[ContentBlock, ...] = ()
    source_revision: str | None = None
    metadata_fingerprint: Sha256
    platform_metadata: dict[str, Any] = Field(default_factory=dict)
    adapter_version: str = Field(min_length=1)

    @field_validator("source_url", "author_url")
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)

    @field_validator("published_at", "first_seen_at", "last_seen_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_datetime(value)

    @field_validator("platform_metadata")
    @classmethod
    def validate_platform_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_metadata(value)
        return value

    @model_validator(mode="after")
    def validate_identity_and_order(self) -> NormalizedItem:
        expected_prefix = f"{self.platform.value}:"
        if not self.canonical_id.startswith(expected_prefix):
            msg = "canonical_id platform prefix does not match platform"
            raise ValueError(msg)
        if self.last_seen_at < self.first_seen_at:
            msg = "last_seen_at must not precede first_seen_at"
            raise ValueError(msg)
        asset_ordinals = [asset.ordinal for asset in self.assets]
        if asset_ordinals != sorted(asset_ordinals) or len(asset_ordinals) != len(
            set(asset_ordinals)
        ):
            msg = "assets must have unique ascending ordinals"
            raise ValueError(msg)
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            msg = "asset IDs must be unique within an item"
            raise ValueError(msg)
        block_ordinals = [block.ordinal for block in self.content_blocks]
        if block_ordinals != sorted(block_ordinals) or len(block_ordinals) != len(
            set(block_ordinals)
        ):
            msg = "content blocks must have unique ascending ordinals"
            raise ValueError(msg)
        return self


class ExtractionRecord(DomainModel):
    extraction_id: str = Field(min_length=1)
    canonical_id: str = Field(min_length=3)
    asset_id: str | None = None
    extraction_type: ExtractionType
    processor_version: str = Field(min_length=1)
    input_fingerprint: Sha256
    config_hash: Sha256
    result_hash: Sha256
    created_at: datetime
    segments: tuple[TextSegment, ...] = ()

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        validated = _validate_aware_datetime(value)
        assert validated is not None
        return validated


class ItemCollectionMembership(DomainModel):
    canonical_id: str = Field(min_length=3)
    collection_canonical_id: str = Field(min_length=3)
    state: MembershipState
    first_seen_at: datetime
    last_seen_at: datetime
    removed_at: datetime | None = None
    last_complete_run_id: str | None = None

    @field_validator("first_seen_at", "last_seen_at", "removed_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_state(self) -> ItemCollectionMembership:
        if self.last_seen_at < self.first_seen_at:
            msg = "last_seen_at must not precede first_seen_at"
            raise ValueError(msg)
        if self.state == MembershipState.REMOVED and self.removed_at is None:
            msg = "removed memberships require removed_at"
            raise ValueError(msg)
        if self.state == MembershipState.ACTIVE and self.removed_at is not None:
            msg = "active memberships must not have removed_at"
            raise ValueError(msg)
        return self
