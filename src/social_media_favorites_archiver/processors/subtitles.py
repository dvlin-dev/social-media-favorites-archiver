"""Normalize platform subtitle payloads into timestamped segments."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Any

from social_media_favorites_archiver.models import TextSegment, TextSource


class SubtitleProvenance(StrEnum):
    HUMAN = "human"
    AI = "ai"
    PLATFORM_NATIVE = "platform_native_unknown"


def _provenance(name: str) -> SubtitleProvenance:
    normalized = name.casefold()
    if any(marker in normalized for marker in ("ai", "自动", "machine", "auto")):
        return SubtitleProvenance.AI
    if any(marker in normalized for marker in ("人工", "human", "manual", "official")):
        return SubtitleProvenance.HUMAN
    return SubtitleProvenance.PLATFORM_NATIVE


def _seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d+):(\d+)[,.](\d+)", value.strip())
    if match is None:
        msg = "invalid SRT timestamp"
        raise ValueError(msg)
    hours, minutes, seconds, fraction = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(fraction.ljust(3, "0")[:3]) / 1000
    )


def parse_srt(
    data: str,
    *,
    language: str,
    provenance: SubtitleProvenance,
    time_offset: float = 0,
    segment_prefix: str = "subtitle",
) -> tuple[TextSegment, ...]:
    """Parse the safe in-memory SRT data returned by yt-dlp."""
    segments: list[TextSegment] = []
    for block_index, block in enumerate(re.split(r"\r?\n\s*\r?\n", data.strip())):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start_text, end_text = (part.strip() for part in lines[timing_index].split("-->", 1))
        text = " ".join(lines[timing_index + 1 :]).strip()
        if not text:
            continue
        start = _seconds(start_text) + time_offset
        end = _seconds(end_text.split()[0]) + time_offset
        identity = hashlib.sha256(
            f"{segment_prefix}:{language}:{block_index}:{start}:{end}:{text}".encode()
        ).hexdigest()[:20]
        segments.append(
            TextSegment(
                segment_id=f"native-{identity}",
                start_time=start,
                end_time=end,
                text=text,
                raw_text=text,
                source=TextSource.NATIVE_SUBTITLE,
                provenance=(provenance.value, f"language:{language}"),
            )
        )
    return tuple(segments)


def normalize_yt_dlp_subtitles(
    subtitles: object,
    *,
    time_offset: float = 0,
    segment_prefix: str = "subtitle",
) -> tuple[TextSegment, ...]:
    """Normalize usable yt-dlp SRT tracks and ignore danmaku/non-text tracks."""
    if not isinstance(subtitles, dict):
        return ()
    segments: list[TextSegment] = []
    for language in sorted(str(key) for key in subtitles):
        tracks = subtitles.get(language)
        if not isinstance(tracks, list):
            continue
        for track_index, track in enumerate(tracks):
            if not isinstance(track, dict) or track.get("ext") != "srt":
                continue
            data: Any = track.get("data")
            if not isinstance(data, str) or not data.strip():
                continue
            name = str(track.get("name") or language)
            segments.extend(
                parse_srt(
                    data,
                    language=language,
                    provenance=_provenance(name),
                    time_offset=time_offset,
                    segment_prefix=f"{segment_prefix}:{track_index}",
                )
            )
    return tuple(sorted(segments, key=lambda segment: (segment.start_time, segment.segment_id)))

