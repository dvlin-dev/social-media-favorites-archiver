from datetime import UTC, datetime
from pathlib import Path

import yaml

from social_media_favorites_archiver.models import (
    Asset,
    AssetKind,
    ContentType,
    MembershipState,
    NormalizedItem,
    Platform,
    TextSegment,
    TextSource,
)
from social_media_favorites_archiver.storage.indexes import IndexEntry, IndexWriter
from social_media_favorites_archiver.storage.markdown import (
    GENERATED_END,
    GENERATED_START,
    MarkdownRenderer,
    NoteRenderContext,
    parse_note,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SHA = f"sha256:{'a' * 64}"


def _item(
    *,
    platform: Platform = Platform.XIAOHONGSHU,
    canonical_id: str = "xiaohongshu:note-001",
    source_url: str = "https://www.xiaohongshu.com/explore/note-001",
) -> NormalizedItem:
    return NormalizedItem(
        canonical_id=canonical_id,
        platform=platform,
        content_type=ContentType.IMAGE_POST,
        source_url=source_url,
        title="Example note",
        author="Example author",
        published_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        original_text="Original example text.",
        assets=(
            Asset(
                asset_id="image-1",
                ordinal=0,
                kind=AssetKind.IMAGE,
                local_path=Path("assets/example/01.jpg"),
                sha256=SHA,
            ),
            Asset(
                asset_id="image-2",
                ordinal=1,
                kind=AssetKind.IMAGE,
                local_path=Path("assets/example/02.jpg"),
                sha256=SHA,
            ),
        ),
        metadata_fingerprint=SHA,
        adapter_version="fixture-v1",
    )


def _context(**updates) -> NoteRenderContext:
    values = {
        "collections": ("Test collection",),
        "favorite_state": MembershipState.ACTIVE,
        "processing_status": "skeleton",
        "first_synced_at": NOW,
        "last_synced_at": NOW,
    }
    values.update(updates)
    return NoteRenderContext.model_validate(values)


def test_skeleton_golden_contains_stable_identity_and_required_metadata(tmp_path: Path) -> None:
    renderer = MarkdownRenderer(tmp_path)

    result = renderer.render(_item(), _context())

    frontmatter, body = parse_note(result.path.read_text(encoding="utf-8"))
    assert result.status == "created"
    assert frontmatter == {
        "smfa_id": "xiaohongshu:note-001",
        "platform": "xiaohongshu",
        "content_type": "image_post",
        "source_url": "https://www.xiaohongshu.com/explore/note-001",
        "author": "Example author",
        "published_at": "2026-08-08T12:00:00+00:00",
        "first_synced_at": "2026-08-08T12:00:00+00:00",
        "last_synced_at": "2026-08-08T12:00:00+00:00",
        "favorite_state": "active",
        "collections": ["Test collection"],
        "tags": [],
        "smfa_generated_tags": [],
        "metadata_fingerprint": SHA,
        "processing_status": "skeleton",
    }
    assert GENERATED_START in body
    assert "Processing status: `skeleton`" in body
    assert "Original example text." in body
    assert "## My notes" in body


def test_completed_note_keeps_images_with_inline_ocr_and_timestamp_provenance(
    tmp_path: Path,
) -> None:
    item = _item(
        platform=Platform.BILIBILI,
        canonical_id="bilibili:BV1example",
        source_url="https://www.bilibili.com/video/BV1example",
    )
    item = item.model_copy(update={"content_type": ContentType.VIDEO})
    context = _context(
        processing_status="complete",
        summary="Summary text.",
        key_points=("First point", "Second point"),
        generated_tags=("generated",),
        transcript=(
            TextSegment(
                segment_id="segment-1",
                start_time=62,
                end_time=65,
                text="Transcript text",
                source=TextSource.ASR,
                provenance=("local-model",),
            ),
        ),
        image_ocr={"image-1": ("OCR one",), "image-2": ("OCR two",)},
    )
    renderer = MarkdownRenderer(tmp_path)

    result = renderer.render(item, context)
    frontmatter, body = parse_note(result.path.read_text(encoding="utf-8"))

    assert frontmatter["processing_status"] == "complete"
    assert frontmatter["tags"] == ["generated"]
    assert "[01:02](https://www.bilibili.com/video/BV1example?t=62)" in body
    assert "Transcript text *(asr; local-model)*" in body
    assert body.index("01.jpg") < body.index("OCR one") < body.index("02.jpg")
    assert body.index("02.jpg") < body.index("OCR two")


def test_resync_preserves_user_text_fields_and_tags(tmp_path: Path) -> None:
    renderer = MarkdownRenderer(tmp_path)
    first = renderer.render(_item(), _context(generated_tags=("old-generated",)))
    frontmatter, body = parse_note(first.path.read_text(encoding="utf-8"))
    frontmatter["custom_field"] = "keep me"
    frontmatter["tags"] = ["user-tag", "old-generated"]
    body = "User preface.\n\n" + body + "\nUser appendix.\n"
    first.path.write_text(
        f"---\n{yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)}---\n{body}",
        encoding="utf-8",
    )

    updated = renderer.render(
        _item(),
        _context(processing_status="complete", generated_tags=("new-generated",)),
    )
    merged, merged_body = parse_note(updated.path.read_text(encoding="utf-8"))

    assert updated.status == "updated"
    assert merged["custom_field"] == "keep me"
    assert merged["tags"] == ["user-tag", "new-generated"]
    assert merged["smfa_generated_tags"] == ["new-generated"]
    assert "User preface." in merged_body
    assert "User appendix." in merged_body


def test_marker_conflict_preserves_existing_file_unchanged(tmp_path: Path) -> None:
    renderer = MarkdownRenderer(tmp_path)
    created = renderer.render(_item(), _context())
    damaged = created.path.read_text(encoding="utf-8").replace(GENERATED_END, "<!-- damaged -->")
    created.path.write_text(damaged, encoding="utf-8")

    result = renderer.render(_item(), _context(processing_status="complete"))

    assert result.status == "conflict"
    assert result.diagnostic_code == "note_conflict.markers"
    assert created.path.read_text(encoding="utf-8") == damaged


def test_non_bilibili_transcript_uses_plain_timestamp(tmp_path: Path) -> None:
    renderer = MarkdownRenderer(tmp_path)
    result = renderer.render(
        _item(),
        _context(
            transcript=(
                TextSegment(
                    segment_id="segment-1",
                    start_time=5,
                    end_time=7,
                    text="Plain timestamp",
                    source=TextSource.OCR,
                ),
            )
        ),
    )

    _, body = parse_note(result.path.read_text(encoding="utf-8"))
    assert "[00:05] Plain timestamp" in body
    assert "?t=5" not in body


def test_indexes_are_deterministic_and_use_obsidian_links(tmp_path: Path) -> None:
    writer = IndexWriter(tmp_path)
    entries = (
        IndexEntry(
            smfa_id="xiaohongshu:note-002",
            title="Zeta",
            note_path=Path("Xiaohongshu/zeta.md"),
            author="Author A",
            collections=("Collection A",),
            tags=("tag-a",),
            topics=("Topic A",),
        ),
        IndexEntry(
            smfa_id="bilibili:BV1example",
            title="Alpha",
            note_path=Path("Bilibili/alpha.md"),
            author="Author A",
            collections=("Collection A",),
            tags=("tag-a",),
            topics=("Topic A",),
        ),
    )

    first_paths = writer.rebuild(entries)
    first_contents = {path: path.read_text(encoding="utf-8") for path in first_paths}
    second_paths = writer.rebuild(tuple(reversed(entries)))
    second_contents = {path: path.read_text(encoding="utf-8") for path in second_paths}

    assert first_contents == second_contents
    author_index = tmp_path / "Indexes" / "Authors" / "Author-A.md"
    content = author_index.read_text(encoding="utf-8")
    assert content.index("[[Bilibili/alpha|Alpha]]") < content.index(
        "[[Xiaohongshu/zeta|Zeta]]"
    )

