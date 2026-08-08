import math
import struct
import threading
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from social_media_favorites_archiver.config import ASRBackend
from social_media_favorites_archiver.models import TextSegment, TextSource
from social_media_favorites_archiver.processors.asr import (
    ASRCancelled,
    ASRRequest,
    ASRResult,
    AudioExtractionError,
    TranscriptionOutcome,
    backend_availability,
    extract_audio,
    is_silent_wav,
    validate_transcript,
)
from social_media_favorites_archiver.processors.terminology import TerminologyCorrector

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _write_wave(path: Path, *, duration: float, frequency: float = 440, channels: int = 1) -> None:
    sample_rate = 22050
    frame_count = int(duration * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            sample = int(8000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", sample) * channels)
        output.writeframes(bytes(frames))


def test_asr_result_requires_explicit_speech_or_verified_no_speech() -> None:
    no_speech = ASRResult(
        outcome=TranscriptionOutcome.VERIFIED_NO_SPEECH,
        backend="fixture",
        model="fixture-model",
        language="zh",
        audio_duration=1.0,
    )
    assert no_speech.segments == ()

    with pytest.raises(ValidationError):
        ASRResult(
            outcome=TranscriptionOutcome.SPEECH,
            backend="fixture",
            model="fixture-model",
            language="zh",
            audio_duration=1.0,
        )


def test_transcript_validation_checks_monotonic_bounded_timestamps() -> None:
    valid = (
        TextSegment(
            segment_id="one",
            start_time=0,
            end_time=1,
            text="First",
            source=TextSource.ASR,
        ),
        TextSegment(
            segment_id="two",
            start_time=1,
            end_time=2,
            text="Second",
            source=TextSource.ASR,
        ),
    )
    validate_transcript(valid, audio_duration=2)

    with pytest.raises(ValueError):
        validate_transcript(tuple(reversed(valid)), audio_duration=2)
    with pytest.raises(ValueError):
        validate_transcript(valid, audio_duration=1.5)


def test_terminology_correction_preserves_raw_text_and_audit_records() -> None:
    corrector = TerminologyCorrector({"错别字": "正确词", "AS R": "ASR"})

    result = corrector.apply("这里有错别字,也有 AS R。")

    assert result.raw_text == "这里有错别字,也有 AS R。"
    assert result.corrected_text == "这里有正确词,也有 ASR。"
    assert [(change.original, change.replacement) for change in result.corrections] == [
        ("错别字", "正确词"),
        ("AS R", "ASR"),
    ]


def test_ffmpeg_extracts_bounded_mono_16khz_audio(tmp_path: Path) -> None:
    source = tmp_path / "stereo.wav"
    target = tmp_path / "mono.wav"
    _write_wave(source, duration=0.25, channels=2)

    extract_audio(source, target, timeout_seconds=10)

    with wave.open(str(target), "rb") as extracted:
        assert extracted.getnchannels() == 1
        assert extracted.getframerate() == 16000


def test_silence_corrupt_cancellation_and_retry_are_safe(tmp_path: Path) -> None:
    silence = tmp_path / "silence.wav"
    with wave.open(str(silence), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 16000)
    assert is_silent_wav(silence)

    corrupt = tmp_path / "corrupt.media"
    corrupt.write_bytes(b"not-media")
    target = tmp_path / "retry.wav"
    with pytest.raises(AudioExtractionError):
        extract_audio(corrupt, target, timeout_seconds=5)
    assert not target.exists()

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ASRCancelled):
        extract_audio(silence, target, timeout_seconds=5, cancel_event=cancelled)

    cancelled.clear()
    extract_audio(silence, target, timeout_seconds=5, cancel_event=cancelled)
    assert target.exists()


def test_generated_short_long_and_multilingual_requests_are_valid(tmp_path: Path) -> None:
    short = tmp_path / "short.wav"
    long = tmp_path / "long.wav"
    _write_wave(short, duration=0.1)
    _write_wave(long, duration=2.0, frequency=660)

    chinese = ASRRequest(language="zh", hotwords=("知识库",), timeout_seconds=30)
    multilingual = ASRRequest(language=None, hotwords=(), timeout_seconds=30)

    assert short.stat().st_size < long.stat().st_size
    assert chinese.hotwords == ("知识库",)
    assert multilingual.language is None


def test_backend_availability_is_actionable_without_cloud_fallback() -> None:
    status = backend_availability(
        ASRBackend.MLX_WHISPER,
        module_finder=lambda _: None,
        command_finder=lambda _: None,
    )

    assert status.available is False
    assert "uv sync --extra asr-mlx" in status.action
    assert "cloud" not in status.action.lower()
