"""Deterministic Obsidian-compatible index pages."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from social_media_favorites_archiver.storage.markdown import (
    GENERATED_END,
    GENERATED_START,
    atomic_write_text,
    safe_filename,
)


class IndexEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    smfa_id: str
    title: str
    note_path: Path
    author: str
    collections: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()


class IndexWriter:
    """Rebuild deterministic author, collection, tag, and topic indexes."""

    def __init__(self, vault_path: str | Path) -> None:
        self.vault_path = Path(vault_path)

    def _note_link(self, entry: IndexEntry) -> str:
        path = entry.note_path
        if path.is_absolute():
            path = path.relative_to(self.vault_path)
        target = path.with_suffix("").as_posix()
        return f"[[{target}|{entry.title}]]"

    def rebuild(self, entries: tuple[IndexEntry, ...]) -> tuple[Path, ...]:
        groupings: dict[str, Callable[[IndexEntry], tuple[str, ...]]] = {
            "Authors": lambda entry: (entry.author,),
            "Collections": lambda entry: entry.collections,
            "Tags": lambda entry: entry.tags,
            "Topics": lambda entry: entry.topics,
        }
        written: list[Path] = []
        for directory, values_for in groupings.items():
            grouped: defaultdict[str, list[IndexEntry]] = defaultdict(list)
            for entry in entries:
                for value in values_for(entry):
                    grouped[value].append(entry)
            for value in sorted(grouped, key=str.casefold):
                grouped_entries = sorted(
                    grouped[value],
                    key=lambda entry: (entry.title.casefold(), entry.smfa_id),
                )
                links = "\n".join(f"- {self._note_link(entry)}" for entry in grouped_entries)
                content = (
                    f"# {value}\n\n{GENERATED_START}\n{links}\n{GENERATED_END}\n"
                )
                path = self.vault_path / "Indexes" / directory / f"{safe_filename(value)}.md"
                atomic_write_text(path, content)
                written.append(path)
        return tuple(sorted(written))
