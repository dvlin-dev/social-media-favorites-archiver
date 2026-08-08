from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, cast

import pytest

from social_media_favorites_archiver.models import TextSegment, TextSource
from social_media_favorites_archiver.processors.fusion import (
    FusionConfig,
    FusionKind,
    fuse_timelines,
    normalize_text,
)


class InputRecord(TypedDict):
    id: str
    start: float
    end: float
    text: str
    source: str


class CaseRecord(TypedDict):
    name: str
    inputs: list[InputRecord]
    expected_kinds: list[str]
    expected_text: list[str]


FIXTURE = Path(__file__).parents[1] / "fixtures" / "sanitized" / "fusion_cases.json"
CASES = cast(dict[str, list[CaseRecord]], json.loads(FIXTURE.read_text()))["cases"]


def _segment(record: InputRecord) -> TextSegment:
    return TextSegment(
        segment_id=record["id"],
        start_time=record["start"],
        end_time=record["end"],
        text=record["text"],
        raw_text=record["text"],
        source=TextSource(record["source"]),
        confidence=0.8,
        provenance=("sanitized-fixture",),
    )


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_fusion_cases(case: CaseRecord) -> None:
    result = fuse_timelines(tuple(_segment(record) for record in case["inputs"]))

    assert [segment.kind.value for segment in result.segments] == case["expected_kinds"]
    assert [segment.text for segment in result.segments] == case["expected_text"]
    assert result.transcript == "\n".join(case["expected_text"])


def test_normalization_is_configurable_and_readings_retain_original_text() -> None:
    original = "臺 灣\uff0c編號\uff5c23"
    segment = TextSegment(
        segment_id="ocr-traditional",
        start_time=0,
        end_time=0,
        text=original,
        raw_text=original,
        source=TextSource.VISUAL_ANNOTATION,
    )
    config = FusionConfig(
        character_variants={"臺": "台", "灣": "湾", "編": "编", "號": "号"}
    )

    result = fuse_timelines((segment,), config=config)

    assert normalize_text(segment.text, config=config) == "台湾编号123"
    assert result.segments[0].readings[0].original_text == original
    assert result.segments[0].readings[0].normalized_text == "台湾编号123"


def test_conflict_keeps_both_readings_and_provenance() -> None:
    case = next(case for case in CASES if case["name"] == "ambiguous conflict")

    result = fuse_timelines(tuple(_segment(record) for record in case["inputs"]))

    conflict = result.segments[0]
    assert conflict.kind == FusionKind.CONFLICT
    assert {reading.original_text for reading in conflict.readings} == {"加入白糖", "加入食盐"}
    assert {reading.source for reading in conflict.readings} == {
        TextSource.ASR,
        TextSource.BURNED_CAPTION,
    }
    assert set(conflict.input_segment_ids) == {"asr-conflict", "ocr-conflict"}


def test_output_is_deterministic_stable_and_maps_every_input_once() -> None:
    inputs = tuple(_segment(record) for case in CASES for record in case["inputs"])

    forward = fuse_timelines(inputs)
    reverse = fuse_timelines(tuple(reversed(inputs)))

    assert forward == reverse
    assert [segment.start_time for segment in forward.segments] == sorted(
        segment.start_time for segment in forward.segments
    )
    output_ids = [segment.segment_id for segment in forward.segments]
    assert len(output_ids) == len(set(output_ids))
    mapped_inputs = [
        input_id for mapping in forward.segment_map for input_id in mapping.input_segment_ids
    ]
    assert sorted(mapped_inputs) == sorted(segment.segment_id for segment in inputs)
    assert len(mapped_inputs) == len(set(mapped_inputs))


def test_nearby_unrelated_visual_text_is_not_silently_merged() -> None:
    inputs = (
        TextSegment(
            segment_id="spoken",
            start_time=0,
            end_time=2,
            text="打开应用",
            source=TextSource.ASR,
        ),
        TextSegment(
            segment_id="code",
            start_time=1,
            end_time=1,
            text="HTTP 404",
            source=TextSource.VISUAL_ANNOTATION,
        ),
    )

    result = fuse_timelines(inputs)

    assert [segment.kind for segment in result.segments] == [
        FusionKind.SPOKEN,
        FusionKind.VISUAL,
    ]
    assert {reading.original_text for segment in result.segments for reading in segment.readings} == {
        "打开应用",
        "HTTP 404",
    }


def test_punctuation_only_ocr_noise_does_not_abort_useful_fusion() -> None:
    useful = TextSegment(
        segment_id="spoken-useful",
        start_time=0,
        end_time=2,
        text="保留有效文本",
        source=TextSource.ASR,
    )
    noise = TextSegment(
        segment_id="ocr-punctuation-noise",
        start_time=1,
        end_time=1,
        text="...!",
        source=TextSource.BURNED_CAPTION,
    )

    result = fuse_timelines((useful, noise))

    assert result.transcript == "保留有效文本"
    assert [entry.input_segment_ids for entry in result.segment_map] == [
        ("spoken-useful",)
    ]
