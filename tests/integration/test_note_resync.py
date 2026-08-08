from datetime import UTC, datetime
from pathlib import Path

from social_media_favorites_archiver.models import ContentType, NormalizedItem, Platform
from social_media_favorites_archiver.storage.markdown import (
    GENERATED_START,
    MarkdownRenderer,
    NoteRenderContext,
    parse_note,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SHA = f"sha256:{'a' * 64}"


def _item() -> NormalizedItem:
    return NormalizedItem(
        canonical_id="bilibili:BV1example",
        platform=Platform.BILIBILI,
        content_type=ContentType.VIDEO,
        source_url="https://www.bilibili.com/video/BV1example",
        title="Original title",
        author="Example author",
        first_seen_at=NOW,
        last_seen_at=NOW,
        metadata_fingerprint=SHA,
        adapter_version="fixture-v1",
    )


def _context(status: str) -> NoteRenderContext:
    return NoteRenderContext(
        collections=("Collection",),
        processing_status=status,
        first_synced_at=NOW,
        last_synced_at=NOW,
    )


def test_moved_note_is_found_by_smfa_id_and_updated_in_place(tmp_path: Path) -> None:
    renderer = MarkdownRenderer(tmp_path)
    created = renderer.render(_item(), _context("skeleton"))
    moved = tmp_path / "Manually Organized" / "Renamed note.md"
    moved.parent.mkdir(parents=True)
    created.path.rename(moved)

    updated = renderer.render(_item(), _context("complete"))

    assert updated.path == moved
    assert not created.path.exists()
    notes = [
        path
        for path in tmp_path.rglob("*.md")
        if parse_note(path.read_text(encoding="utf-8"))[0].get("smfa_id") == _item().canonical_id
    ]
    assert notes == [moved]
    assert parse_note(moved.read_text(encoding="utf-8"))[0]["processing_status"] == "complete"


def test_corrupted_moved_note_is_not_overwritten(tmp_path: Path) -> None:
    renderer = MarkdownRenderer(tmp_path)
    created = renderer.render(_item(), _context("skeleton"))
    damaged = created.path.read_text(encoding="utf-8").replace(GENERATED_START, "")
    created.path.write_text(damaged, encoding="utf-8")

    result = renderer.render(_item(), _context("complete"))

    assert result.status == "conflict"
    assert created.path.read_text(encoding="utf-8") == damaged
