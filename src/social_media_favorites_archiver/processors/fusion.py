"""Deterministic, auditable fusion of spoken and visual text timelines."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rapidfuzz.fuzz import ratio

from social_media_favorites_archiver.models import TextSegment, TextSource


class FusionKind(StrEnum):
    SPOKEN = "spoken"
    VISUAL = "visual"
    CONFLICT = "conflict"


class MappingAction(StrEnum):
    MERGED = "merged"
    PRESERVED = "preserved"
    CONFLICT = "conflict"


class FusionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    similarity_threshold: float = Field(default=85, ge=0, le=100)
    conflict_threshold: float = Field(default=40, ge=0, le=100)
    nearby_seconds: float = Field(default=0.75, ge=0)
    repeated_caption_gap_seconds: float = Field(default=2.5, ge=0)
    character_variants: dict[str, str] = Field(default_factory=dict)
    ocr_confusions: dict[str, str] = Field(
        default_factory=lambda: {"|": "1", "丨": "1"}
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> FusionConfig:
        if self.conflict_threshold >= self.similarity_threshold:
            msg = "conflict threshold must be lower than similarity threshold"
            raise ValueError(msg)
        return self


class SourceReading(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_segment_id: str = Field(min_length=1)
    original_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    source: TextSource
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: tuple[str, ...] = ()


class FusedSegment(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_id: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    text: str = Field(min_length=1)
    kind: FusionKind
    confidence: float | None = Field(default=None, ge=0, le=1)
    input_segment_ids: tuple[str, ...]
    readings: tuple[SourceReading, ...]
    provenance: tuple[str, ...]

    @model_validator(mode="after")
    def validate_segment(self) -> FusedSegment:
        if self.end_time < self.start_time:
            msg = "fused segment end time must not precede start time"
            raise ValueError(msg)
        if not self.input_segment_ids or not self.readings:
            msg = "fused segments require source readings"
            raise ValueError(msg)
        reading_ids = tuple(reading.input_segment_id for reading in self.readings)
        if reading_ids != self.input_segment_ids:
            msg = "fused segment readings must match its input segment map"
            raise ValueError(msg)
        return self


class SegmentMapEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    output_segment_id: str = Field(min_length=1)
    input_segment_ids: tuple[str, ...]
    action: MappingAction


class FusionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    segments: tuple[FusedSegment, ...]
    segment_map: tuple[SegmentMapEntry, ...]
    transcript: str

    @model_validator(mode="after")
    def validate_result(self) -> FusionResult:
        segment_ids = tuple(segment.segment_id for segment in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            msg = "fusion produced duplicate segment IDs"
            raise ValueError(msg)
        mapped_outputs = tuple(mapping.output_segment_id for mapping in self.segment_map)
        if segment_ids != mapped_outputs:
            msg = "fusion segment map is not aligned with output segments"
            raise ValueError(msg)
        mapped_inputs = [
            input_id for mapping in self.segment_map for input_id in mapping.input_segment_ids
        ]
        if len(mapped_inputs) != len(set(mapped_inputs)):
            msg = "an input segment may only map to one fused segment"
            raise ValueError(msg)
        return self


@dataclass
class _VisualGroup:
    segments: list[TextSegment]
    normalized_text: str
    source: TextSource

    @property
    def start_time(self) -> float:
        return min(segment.start_time for segment in self.segments)

    @property
    def end_time(self) -> float:
        return max(segment.end_time for segment in self.segments)


_SPOKEN_SOURCES = {TextSource.ASR, TextSource.NATIVE_SUBTITLE}
_SOURCE_PRIORITY = {
    TextSource.NATIVE_SUBTITLE: 0,
    TextSource.ASR: 1,
    TextSource.BURNED_CAPTION: 2,
    TextSource.VISUAL_ANNOTATION: 3,
    TextSource.OCR: 4,
}
_KIND_PRIORITY = {FusionKind.SPOKEN: 0, FusionKind.CONFLICT: 1, FusionKind.VISUAL: 2}


def normalize_text(text: str, *, config: FusionConfig | None = None) -> str:
    """Normalize comparison text while leaving source readings untouched."""
    settings = config or FusionConfig()
    normalized = unicodedata.normalize("NFKC", text)
    for original, replacement in settings.character_variants.items():
        normalized = normalized.replace(original, replacement)
    for original, replacement in settings.ocr_confusions.items():
        normalized = normalized.replace(original, replacement)
    normalized = normalized.casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "Z"))
    )


def _segment_key(segment: TextSegment) -> tuple[float, float, int, str]:
    return (
        segment.start_time,
        segment.end_time,
        _SOURCE_PRIORITY[segment.source],
        segment.segment_id,
    )


def _collapse_visual_segments(
    segments: tuple[TextSegment, ...],
    *,
    config: FusionConfig,
) -> list[_VisualGroup]:
    groups: list[_VisualGroup] = []
    for segment in sorted(segments, key=_segment_key):
        normalized = normalize_text(segment.text, config=config)
        if not normalized:
            continue
        matching = next(
            (
                group
                for group in reversed(groups)
                if group.normalized_text == normalized
                and group.source == segment.source
                and segment.start_time - group.end_time
                <= config.repeated_caption_gap_seconds
            ),
            None,
        )
        if matching is None:
            groups.append(
                _VisualGroup(
                    segments=[segment],
                    normalized_text=normalized,
                    source=segment.source,
                )
            )
        else:
            matching.segments.append(segment)
    return groups


def _temporally_near(
    spoken: TextSegment,
    visual: _VisualGroup,
    *,
    nearby_seconds: float,
) -> bool:
    gap = max(
        spoken.start_time - visual.end_time,
        visual.start_time - spoken.end_time,
        0,
    )
    return gap <= nearby_seconds


def _reading(segment: TextSegment, *, config: FusionConfig) -> SourceReading:
    original = segment.raw_text or segment.text
    return SourceReading(
        input_segment_id=segment.segment_id,
        original_text=original,
        normalized_text=normalize_text(segment.text, config=config),
        source=segment.source,
        confidence=segment.confidence,
        provenance=segment.provenance,
    )


def _identity(kind: FusionKind, segments: tuple[TextSegment, ...]) -> str:
    input_ids = ":".join(sorted(segment.segment_id for segment in segments))
    digest = hashlib.sha256(f"{kind.value}:{input_ids}".encode()).hexdigest()[:20]
    return f"fusion-{digest}"


def _build_segment(
    segments: tuple[TextSegment, ...],
    *,
    kind: FusionKind,
    action: MappingAction,
    primary: TextSegment,
    config: FusionConfig,
) -> tuple[FusedSegment, SegmentMapEntry]:
    ordered = tuple(sorted(segments, key=_segment_key))
    readings = tuple(_reading(segment, config=config) for segment in ordered)
    segment_id = _identity(kind, ordered)
    confidences = [segment.confidence for segment in ordered if segment.confidence is not None]
    fused = FusedSegment(
        segment_id=segment_id,
        start_time=min(segment.start_time for segment in ordered),
        end_time=max(segment.end_time for segment in ordered),
        text=primary.text,
        kind=kind,
        confidence=max(confidences) if confidences else None,
        input_segment_ids=tuple(reading.input_segment_id for reading in readings),
        readings=readings,
        provenance=tuple(
            dict.fromkeys(
                (
                    "fusion:deterministic-v1",
                    *(item for segment in ordered for item in segment.provenance),
                )
            )
        ),
    )
    return fused, SegmentMapEntry(
        output_segment_id=segment_id,
        input_segment_ids=fused.input_segment_ids,
        action=action,
    )


def fuse_timelines(
    segments: tuple[TextSegment, ...],
    *,
    config: FusionConfig | None = None,
) -> FusionResult:
    """Fuse ASR/subtitles with frame OCR without unexplained source-text loss."""
    settings = config or FusionConfig()
    input_ids = [segment.segment_id for segment in segments]
    if len(input_ids) != len(set(input_ids)):
        raise ValueError("fusion input segment IDs must be unique")
    segments = tuple(
        segment
        for segment in segments
        if normalize_text(segment.text, config=settings)
    )

    spoken_segments = tuple(
        segment for segment in segments if segment.source in _SPOKEN_SOURCES
    )
    visual_segments = tuple(
        segment for segment in segments if segment.source not in _SPOKEN_SOURCES
    )
    visual_groups = _collapse_visual_segments(visual_segments, config=settings)
    used_groups: set[int] = set()
    output: list[tuple[FusedSegment, SegmentMapEntry]] = []

    for spoken in sorted(spoken_segments, key=_segment_key):
        spoken_normalized = normalize_text(spoken.text, config=settings)
        candidates: list[tuple[float, int, _VisualGroup]] = []
        for index, group in enumerate(visual_groups):
            if index in used_groups or not _temporally_near(
                spoken, group, nearby_seconds=settings.nearby_seconds
            ):
                continue
            similarity = ratio(spoken_normalized, group.normalized_text)
            candidates.append((similarity, index, group))
        candidates.sort(
            key=lambda item: (
                -item[0],
                item[2].start_time,
                tuple(segment.segment_id for segment in item[2].segments),
            )
        )
        best = candidates[0] if candidates else None
        if best is not None and best[0] >= settings.similarity_threshold:
            _, index, group = best
            used_groups.add(index)
            output.append(
                _build_segment(
                    (spoken, *group.segments),
                    kind=FusionKind.SPOKEN,
                    action=MappingAction.MERGED,
                    primary=spoken,
                    config=settings,
                )
            )
        elif (
            best is not None
            and best[0] >= settings.conflict_threshold
            and best[2].source == TextSource.BURNED_CAPTION
        ):
            _, index, group = best
            used_groups.add(index)
            output.append(
                _build_segment(
                    (spoken, *group.segments),
                    kind=FusionKind.CONFLICT,
                    action=MappingAction.CONFLICT,
                    primary=spoken,
                    config=settings,
                )
            )
        else:
            output.append(
                _build_segment(
                    (spoken,),
                    kind=FusionKind.SPOKEN,
                    action=MappingAction.PRESERVED,
                    primary=spoken,
                    config=settings,
                )
            )

    for index, group in enumerate(visual_groups):
        if index in used_groups:
            continue
        primary = min(group.segments, key=_segment_key)
        output.append(
            _build_segment(
                tuple(group.segments),
                kind=FusionKind.VISUAL,
                action=MappingAction.PRESERVED,
                primary=primary,
                config=settings,
            )
        )

    output.sort(
        key=lambda item: (
            item[0].start_time,
            _KIND_PRIORITY[item[0].kind],
            item[0].segment_id,
        )
    )
    fused_segments = tuple(item[0] for item in output)
    segment_map = tuple(item[1] for item in output)
    return FusionResult(
        segments=fused_segments,
        segment_map=segment_map,
        transcript="\n".join(segment.text for segment in fused_segments),
    )
