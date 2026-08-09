"""Local timestamped ASR protocols and backend adapters."""

from __future__ import annotations

import importlib.util
import math
import os
import shutil
import subprocess
import sys
import threading
import wave
from array import array
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from social_media_favorites_archiver.config import ASRBackend
from social_media_favorites_archiver.models import TextSegment, TextSource
from social_media_favorites_archiver.processors.terminology import (
    TerminologyCorrection,
    TerminologyCorrector,
)
from social_media_favorites_archiver.safety.redaction import redact_text


class TranscriptionOutcome(StrEnum):
    SPEECH = "speech"
    VERIFIED_NO_SPEECH = "verified_no_speech"


class ASRRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    language: str | None = None
    hotwords: tuple[str, ...] = ()
    terminology: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=600, gt=0)


class ASRResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: TranscriptionOutcome
    backend: str = Field(min_length=1)
    model: str = Field(min_length=1)
    language: str | None
    audio_duration: float = Field(ge=0)
    segments: tuple[TextSegment, ...] = ()
    raw_text: str = ""
    corrected_text: str = ""
    corrections: tuple[TerminologyCorrection, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> ASRResult:
        if self.outcome == TranscriptionOutcome.SPEECH and not self.segments:
            msg = "speech outcomes require timestamped segments"
            raise ValueError(msg)
        if self.outcome == TranscriptionOutcome.VERIFIED_NO_SPEECH and self.segments:
            msg = "verified no-speech outcomes must not contain segments"
            raise ValueError(msg)
        validate_transcript(self.segments, audio_duration=self.audio_duration)
        return self


class BackendAvailability(BaseModel):
    model_config = ConfigDict(frozen=True)

    backend: ASRBackend
    available: bool
    action: str


class AudioExtractionError(RuntimeError):
    """Raised with a sanitized FFmpeg failure summary."""


class ASRCancelled(RuntimeError):
    """Raised when local processing is cancelled at a safe checkpoint."""


class BackendUnavailableError(RuntimeError):
    """Raised when a selected local backend is not installed."""


class LocalASRBackend(Protocol):
    name: str
    model: str

    def transcribe(
        self,
        audio_path: Path,
        request: ASRRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ASRResult: ...


def validate_transcript(segments: tuple[TextSegment, ...], *, audio_duration: float) -> None:
    previous_start = -1.0
    for segment in segments:
        if segment.start_time < previous_start:
            msg = "transcript segment timestamps are not monotonic"
            raise ValueError(msg)
        if segment.end_time > audio_duration + 0.05:
            msg = "transcript segment extends beyond the audio duration"
            raise ValueError(msg)
        previous_start = segment.start_time


def audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as source:
            frame_rate = source.getframerate()
            if frame_rate <= 0:
                raise AudioExtractionError("audio has an invalid frame rate")
            return source.getnframes() / frame_rate
    except (OSError, wave.Error) as error:
        raise AudioExtractionError("audio duration could not be verified") from error


def is_silent_wav(path: Path, *, rms_threshold: float = 30) -> bool:
    """Use a deterministic PCM RMS guard before invoking a model."""
    try:
        with wave.open(str(path), "rb") as source:
            if source.getsampwidth() != 2:
                return False
            frames = source.readframes(source.getnframes())
    except (OSError, wave.Error):
        return False
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return True
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return math.sqrt(mean_square) <= rms_threshold


def extract_audio(
    source: str | Path,
    target: str | Path,
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
    ffmpeg_executable: str = "ffmpeg",
) -> Path:
    """Extract checked mono 16 kHz PCM audio through a bounded subprocess."""
    if cancel_event is not None and cancel_event.is_set():
        raise ASRCancelled("audio extraction cancelled before launch")
    source_path = Path(source)
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    partial = target_path.with_name(f"{target_path.stem}.partial.wav")
    partial.unlink(missing_ok=True)
    command = [
        ffmpeg_executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(partial),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise ASRCancelled("audio extraction cancelled after FFmpeg completed")
        if not partial.is_file() or audio_duration(partial) < 0:
            raise AudioExtractionError("FFmpeg did not produce a verifiable audio file")
        os.replace(partial, target_path)
        return target_path
    except ASRCancelled:
        partial.unlink(missing_ok=True)
        raise
    except subprocess.TimeoutExpired as error:
        partial.unlink(missing_ok=True)
        raise AudioExtractionError("FFmpeg audio extraction timed out") from error
    except subprocess.CalledProcessError as error:
        partial.unlink(missing_ok=True)
        summary = redact_text((error.stderr or "FFmpeg rejected the input")[-500:])
        raise AudioExtractionError(f"FFmpeg audio extraction failed: {summary}") from error
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise AudioExtractionError("FFmpeg could not be executed") from error


def backend_availability(
    backend: ASRBackend,
    *,
    module_finder: Callable[[str], object | None] = importlib.util.find_spec,
    command_finder: Callable[[str], str | None] = shutil.which,
) -> BackendAvailability:
    checks: dict[ASRBackend, tuple[bool, str]] = {
        ASRBackend.FUNASR: (
            module_finder("funasr") is not None,
            "Install locally with `uv sync --extra asr-funasr`.",
        ),
        ASRBackend.MLX_WHISPER: (
            module_finder("mlx_whisper") is not None,
            "Install locally with `uv sync --extra asr-mlx`.",
        ),
        ASRBackend.FASTER_WHISPER: (
            module_finder("faster_whisper") is not None,
            "Install locally with `uv sync --extra asr-faster-whisper`.",
        ),
        ASRBackend.WHISPER_CPP: (
            command_finder("whisper-cli") is not None,
            "Install a local whisper.cpp `whisper-cli` and configure its model path.",
        ),
    }
    if backend not in checks:
        return BackendAvailability(
            backend=backend,
            available=False,
            action="Select an explicit installed local ASR backend.",
        )
    available, action = checks[backend]
    return BackendAvailability(
        backend=backend,
        available=available,
        action="Local backend is available." if available else action,
    )


def _no_speech(backend: str, model: str, language: str | None, duration: float) -> ASRResult:
    return ASRResult(
        outcome=TranscriptionOutcome.VERIFIED_NO_SPEECH,
        backend=backend,
        model=model,
        language=language,
        audio_duration=duration,
    )


def _result_from_segments(
    raw_segments: list[dict[str, object]],
    *,
    backend: str,
    model: str,
    language: str | None,
    duration: float,
    terminology: Mapping[str, str],
) -> ASRResult:
    corrector = TerminologyCorrector(terminology)
    segments: list[TextSegment] = []
    all_corrections: list[TerminologyCorrection] = []
    raw_text_parts: list[str] = []
    corrected_text_parts: list[str] = []
    for index, raw_segment in enumerate(raw_segments):
        text = str(raw_segment.get("text") or "").strip()
        if not text:
            continue
        start_value = raw_segment.get("start")
        end_value = raw_segment.get("end")
        start = float(start_value) if isinstance(start_value, (int, float)) else 0.0
        end = min(
            duration,
            float(end_value) if isinstance(end_value, (int, float)) else start,
        )
        correction = corrector.apply(text)
        confidence_value = raw_segment.get("confidence")
        confidence = (
            min(1.0, max(0.0, float(confidence_value)))
            if isinstance(confidence_value, (int, float))
            else None
        )
        segments.append(
            TextSegment(
                segment_id=f"asr-{backend}-{index:05d}",
                start_time=start,
                end_time=end,
                text=correction.corrected_text,
                raw_text=correction.raw_text,
                source=TextSource.ASR,
                confidence=confidence,
                provenance=(backend, model, f"language:{language or 'auto'}"),
            )
        )
        raw_text_parts.append(correction.raw_text)
        corrected_text_parts.append(correction.corrected_text)
        all_corrections.extend(correction.corrections)
    normalized = tuple(sorted(segments, key=lambda segment: (segment.start_time, segment.segment_id)))
    if not normalized:
        return _no_speech(backend, model, language, duration)
    validate_transcript(normalized, audio_duration=duration)
    return ASRResult(
        outcome=TranscriptionOutcome.SPEECH,
        backend=backend,
        model=model,
        language=language,
        audio_duration=duration,
        segments=normalized,
        raw_text="\n".join(raw_text_parts),
        corrected_text="\n".join(corrected_text_parts),
        corrections=tuple(all_corrections),
    )


class MlxWhisperBackend:
    name = "mlx-whisper"

    def __init__(self, *, model: str) -> None:
        self.model = model

    def transcribe(
        self,
        audio_path: Path,
        request: ASRRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ASRResult:
        if cancel_event is not None and cancel_event.is_set():
            raise ASRCancelled("MLX transcription cancelled before model invocation")
        if importlib.util.find_spec("mlx_whisper") is None:
            raise BackendUnavailableError(backend_availability(ASRBackend.MLX_WHISPER).action)
        duration = audio_duration(audio_path)
        if is_silent_wav(audio_path):
            return _no_speech(self.name, self.model, request.language, duration)
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=self.model,
            language=request.language,
            initial_prompt=" ".join(request.hotwords) or None,
            verbose=False,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise ASRCancelled("MLX transcription cancelled after model invocation")
        raw_segments: list[dict[str, object]] = []
        for segment in result.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            average_log_probability = segment.get("avg_logprob")
            confidence = (
                math.exp(float(average_log_probability))
                if isinstance(average_log_probability, (int, float))
                else None
            )
            raw_segments.append(
                {
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "text": segment.get("text"),
                    "confidence": confidence,
                }
            )
        return _result_from_segments(
            raw_segments,
            backend=self.name,
            model=self.model,
            language=request.language or cast_language(result.get("language")),
            duration=duration,
            terminology=request.terminology,
        )


def cast_language(value: object) -> str | None:
    return value if isinstance(value, str) else None


class FunASRBackend:
    name = "funasr"

    def __init__(self, *, model: str) -> None:
        self.model = model

    def transcribe(
        self,
        audio_path: Path,
        request: ASRRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ASRResult:
        if importlib.util.find_spec("funasr") is None:
            raise BackendUnavailableError(backend_availability(ASRBackend.FUNASR).action)
        if cancel_event is not None and cancel_event.is_set():
            raise ASRCancelled("FunASR transcription cancelled")
        from funasr import AutoModel  # type: ignore[import-not-found]

        duration = audio_duration(audio_path)
        if is_silent_wav(audio_path):
            return _no_speech(self.name, self.model, request.language, duration)
        model = AutoModel(model=self.model)
        generated = model.generate(
            input=str(audio_path),
            hotword=" ".join(request.hotwords) or None,
        )
        text = ""
        if isinstance(generated, list) and generated and isinstance(generated[0], dict):
            text = str(generated[0].get("text") or "")
        return _result_from_segments(
            [{"start": 0, "end": duration, "text": text}],
            backend=self.name,
            model=self.model,
            language=request.language,
            duration=duration,
            terminology=request.terminology,
        )


class FasterWhisperBackend:
    name = "faster-whisper"

    def __init__(self, *, model: str) -> None:
        self.model = model

    def transcribe(
        self,
        audio_path: Path,
        request: ASRRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ASRResult:
        if importlib.util.find_spec("faster_whisper") is None:
            raise BackendUnavailableError(backend_availability(ASRBackend.FASTER_WHISPER).action)
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        duration = audio_duration(audio_path)
        if is_silent_wav(audio_path):
            return _no_speech(self.name, self.model, request.language, duration)
        model = WhisperModel(self.model)
        generated, info = model.transcribe(
            str(audio_path),
            language=request.language,
            hotwords=" ".join(request.hotwords) or None,
        )
        raw_segments = [
            {"start": segment.start, "end": segment.end, "text": segment.text}
            for segment in generated
        ]
        return _result_from_segments(
            raw_segments,
            backend=self.name,
            model=self.model,
            language=request.language or cast_language(getattr(info, "language", None)),
            duration=duration,
            terminology=request.terminology,
        )


class WhisperCppBackend:
    name = "whisper-cpp"

    def __init__(self, *, model: str, executable: str = "whisper-cli") -> None:
        self.model = model
        self.executable = executable

    def transcribe(
        self,
        audio_path: Path,
        request: ASRRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ASRResult:
        if shutil.which(self.executable) is None:
            raise BackendUnavailableError(backend_availability(ASRBackend.WHISPER_CPP).action)
        if cancel_event is not None and cancel_event.is_set():
            raise ASRCancelled("whisper.cpp transcription cancelled")
        duration = audio_duration(audio_path)
        if is_silent_wav(audio_path):
            return _no_speech(self.name, self.model, request.language, duration)
        output_prefix = audio_path.with_suffix(".whisper-cpp")
        command = [
            self.executable,
            "-m",
            self.model,
            "-f",
            str(audio_path),
            "-oj",
            "-of",
            str(output_prefix),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=request.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as error:
            raise AudioExtractionError("whisper.cpp local transcription failed") from error
        json_path = Path(f"{output_prefix}.json")
        if not json_path.is_file():
            raise AudioExtractionError("whisper.cpp did not produce JSON output")
        import json

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        raw_segments = [
            {
                "start": segment.get("offsets", {}).get("from", 0) / 1000,
                "end": segment.get("offsets", {}).get("to", 0) / 1000,
                "text": segment.get("text"),
            }
            for segment in payload.get("transcription", [])
            if isinstance(segment, dict)
        ]
        return _result_from_segments(
            raw_segments,
            backend=self.name,
            model=self.model,
            language=request.language,
            duration=duration,
            terminology=request.terminology,
        )
