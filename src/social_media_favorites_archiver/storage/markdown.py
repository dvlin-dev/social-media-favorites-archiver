"""Protected Markdown notes with stable identity and atomic updates."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    MembershipState,
    NormalizedItem,
    Platform,
    TextSegment,
)
from social_media_favorites_archiver.queue import ItemFileLocks

GENERATED_START = "<!-- smfa:generated:start -->"
GENERATED_END = "<!-- smfa:generated:end -->"


class NoteParseError(ValueError):
    """Raised when a note cannot be safely parsed."""


class NoteRenderContext(BaseModel):
    """Derived values supplied by orchestration and processors."""

    model_config = ConfigDict(frozen=True)

    collections: tuple[str, ...] = ()
    favorite_state: MembershipState = MembershipState.ACTIVE
    processing_status: str = Field(min_length=1)
    first_synced_at: datetime
    last_synced_at: datetime
    summary: str | None = None
    key_points: tuple[str, ...] = ()
    generated_tags: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    transcript: tuple[TextSegment, ...] = ()
    image_ocr: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("first_synced_at", "last_synced_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "sync timestamps must include timezone information"
            raise ValueError(msg)
        return value


class RenderResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["created", "updated", "conflict"]
    path: Path
    diagnostic_code: str | None = None


def parse_note(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and body without accepting malformed boundaries."""
    if not text.startswith("---\n"):
        raise NoteParseError("note does not start with YAML frontmatter")
    boundary = text.find("\n---\n", 4)
    if boundary < 0:
        raise NoteParseError("note frontmatter is not terminated")
    try:
        loaded = yaml.safe_load(text[4:boundary])
    except yaml.YAMLError as error:
        raise NoteParseError("note frontmatter is invalid YAML") from error
    if loaded is None:
        frontmatter: dict[str, Any] = {}
    elif isinstance(loaded, dict) and all(isinstance(key, str) for key in loaded):
        frontmatter = dict(loaded)
    else:
        raise NoteParseError("note frontmatter must be a string-keyed mapping")
    return frontmatter, text[boundary + 5 :]


def safe_filename(value: str, *, fallback: str = "untitled") -> str:
    """Create a deterministic readable filename component."""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value)
    cleaned = re.sub(r"\s+", "-", cleaned.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-. ")
    return (cleaned or fallback)[:100]


def atomic_write_text(path: Path, text: str) -> None:
    """Write, fsync, and atomically replace a single file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".smfa-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _deduplicate(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"
    return f"{minutes:02d}:{seconds_part:02d}"


def _bilibili_timestamp_url(source_url: str, seconds: float) -> str:
    parsed = urlsplit(source_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["t"] = str(max(0, int(seconds)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


class MarkdownRenderer:
    """Render skeleton and complete notes while preserving user-owned regions."""

    _PLATFORM_DIRECTORIES: ClassVar[dict[Platform, str]] = {
        Platform.BILIBILI: "Bilibili",
        Platform.XIAOHONGSHU: "Xiaohongshu",
        Platform.DOUYIN: "Douyin",
    }

    def __init__(self, vault_path: str | Path) -> None:
        self.vault_path = Path(vault_path)
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self.file_locks = ItemFileLocks(
            self.vault_path / ".social-media-favorites-archiver" / "locks"
        )

    def _scan_identity_index(self) -> tuple[dict[str, Path], set[str]]:
        index: dict[str, Path] = {}
        duplicates: set[str] = set()
        for path in sorted(self.vault_path.rglob("*.md")):
            try:
                frontmatter, _ = parse_note(path.read_text(encoding="utf-8"))
            except (OSError, NoteParseError):
                continue
            identity = frontmatter.get("smfa_id")
            if not isinstance(identity, str):
                continue
            if identity in index:
                duplicates.add(identity)
            else:
                index[identity] = path
        return index, duplicates

    def _default_path(self, item: NormalizedItem) -> Path:
        short_id = safe_filename(item.canonical_id.split(":", 1)[1], fallback="item")[-20:]
        filename = f"{safe_filename(item.title)}-{short_id}.md"
        return self.vault_path / self._PLATFORM_DIRECTORIES[item.platform] / filename

    def _frontmatter(
        self,
        item: NormalizedItem,
        context: NoteRenderContext,
        existing: Mapping[str, Any],
    ) -> dict[str, Any]:
        merged = dict(existing)
        old_generated = set(_string_list(existing.get("smfa_generated_tags")))
        user_tags = [tag for tag in _string_list(existing.get("tags")) if tag not in old_generated]
        generated_tags = _deduplicate(context.generated_tags)
        merged.update(
            {
                "smfa_id": item.canonical_id,
                "platform": item.platform.value,
                "content_type": item.content_type.value,
                "source_url": item.source_url,
                "author": item.author,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "first_synced_at": context.first_synced_at.isoformat(),
                "last_synced_at": context.last_synced_at.isoformat(),
                "favorite_state": context.favorite_state.value,
                "collections": list(context.collections),
                "tags": _deduplicate((*user_tags, *generated_tags)),
                "smfa_generated_tags": generated_tags,
                "metadata_fingerprint": item.metadata_fingerprint,
                "processing_status": context.processing_status,
            }
        )
        return merged

    def _asset_reference(self, asset: Asset) -> str:
        if asset.local_path is None:
            return f"assets/{safe_filename(asset.asset_id)}"
        if asset.local_path.is_absolute():
            try:
                return asset.local_path.relative_to(self.vault_path).as_posix()
            except ValueError:
                return f"assets/{safe_filename(asset.asset_id)}"
        return asset.local_path.as_posix()

    def _transcript_line(self, item: NormalizedItem, segment: TextSegment) -> str:
        timestamp = _format_timestamp(segment.start_time)
        if item.platform == Platform.BILIBILI:
            timestamp_part = f"[{timestamp}]({_bilibili_timestamp_url(item.source_url, segment.start_time)})"
        else:
            timestamp_part = f"[{timestamp}]"
        provenance = "; ".join((segment.source.value, *segment.provenance))
        return f"- {timestamp_part} {segment.text} *({provenance})*"

    def _generated_content(self, item: NormalizedItem, context: NoteRenderContext) -> str:
        lines = [
            f"# {item.title}",
            "",
            f"Processing status: `{context.processing_status}`",
            "",
        ]
        if context.summary:
            lines.extend(("## Summary", "", context.summary, ""))
        if context.key_points:
            lines.extend(("## Key points", ""))
            lines.extend(f"- {point}" for point in context.key_points)
            lines.append("")
        lines.extend(("## Original text", "", item.original_text or "_No source text._", ""))
        if context.transcript:
            lines.extend(("## Transcript", ""))
            lines.extend(self._transcript_line(item, segment) for segment in context.transcript)
            lines.append("")
        image_assets = [asset for asset in item.assets if asset.kind == AssetKind.IMAGE]
        if image_assets:
            lines.extend(("## Images and OCR", ""))
            for asset in image_assets:
                lines.append(f"![{asset.asset_id}]({self._asset_reference(asset)})")
                ocr_blocks = context.image_ocr.get(asset.asset_id, ())
                if ocr_blocks:
                    lines.append("")
                    lines.append(f"> OCR for `{asset.asset_id}`")
                    for block in ocr_blocks:
                        lines.append(f"> {block}")
                lines.append("")
        other_assets = [asset for asset in item.assets if asset.kind != AssetKind.IMAGE]
        if other_assets:
            lines.extend(("## Local attachments", ""))
            for asset in other_assets:
                lines.append(f"- [{asset.kind.value}]({self._asset_reference(asset)})")
            lines.append("")
        lines.extend(
            (
                "## Source",
                "",
                f"- Platform: `{item.platform.value}`",
                f"- Author: {item.author}",
                f"- Original: {item.source_url}",
                f"- Adapter provenance: `{item.adapter_version}`",
                "",
            )
        )
        if context.collections or context.generated_tags or context.topics:
            lines.extend(("## Relationships", ""))
            lines.extend(f"- Collection: [[{name}]]" for name in context.collections)
            lines.extend(f"- Tag: [[{tag}]]" for tag in context.generated_tags)
            lines.extend(f"- Topic: [[{topic}]]" for topic in context.topics)
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _replace_generated_region(body: str, generated: str) -> str | None:
        if body.count(GENERATED_START) != 1 or body.count(GENERATED_END) != 1:
            return None
        start = body.index(GENERATED_START)
        end = body.index(GENERATED_END)
        if end < start:
            return None
        replacement = f"{GENERATED_START}\n{generated}\n{GENERATED_END}"
        return body[:start] + replacement + body[end + len(GENERATED_END) :]

    @staticmethod
    def _serialize(frontmatter: Mapping[str, Any], body: str) -> str:
        dumped = yaml.safe_dump(
            dict(frontmatter),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).rstrip()
        return f"---\n{dumped}\n---\n{body}"

    def render(self, item: NormalizedItem, context: NoteRenderContext) -> RenderResult:
        with self.file_locks.acquire(item.canonical_id):
            index, duplicates = self._scan_identity_index()
            if item.canonical_id in duplicates:
                return RenderResult(
                    status="conflict",
                    path=index[item.canonical_id],
                    diagnostic_code="note_conflict.duplicate_identity",
                )
            existing_path = index.get(item.canonical_id)
            path = existing_path or self._default_path(item)
            existing_frontmatter: dict[str, Any] = {}
            if existing_path is not None:
                original = existing_path.read_text(encoding="utf-8")
                try:
                    existing_frontmatter, body = parse_note(original)
                except NoteParseError:
                    return RenderResult(
                        status="conflict",
                        path=existing_path,
                        diagnostic_code="note_conflict.frontmatter",
                    )
                if existing_frontmatter.get("smfa_id") != item.canonical_id:
                    return RenderResult(
                        status="conflict",
                        path=existing_path,
                        diagnostic_code="note_conflict.identity",
                    )
                updated_body = self._replace_generated_region(
                    body,
                    self._generated_content(item, context),
                )
                if updated_body is None:
                    return RenderResult(
                        status="conflict",
                        path=existing_path,
                        diagnostic_code="note_conflict.markers",
                    )
                status: Literal["created", "updated"] = "updated"
            else:
                if path.exists():
                    return RenderResult(
                        status="conflict",
                        path=path,
                        diagnostic_code="note_conflict.path",
                    )
                generated = self._generated_content(item, context)
                updated_body = (
                    f"{GENERATED_START}\n{generated}\n{GENERATED_END}\n\n"
                    "## My notes\n\n"
                )
                status = "created"
            frontmatter = self._frontmatter(item, context, existing_frontmatter)
            atomic_write_text(path, self._serialize(frontmatter, updated_body))
            return RenderResult(status=status, path=path)
