"""Adaptive video frames from scene changes, intervals, and text-region changes."""

from __future__ import annotations

import re
import subprocess
from enum import StrEnum
from pathlib import Path

import imagehash
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from social_media_favorites_archiver.safety.redaction import redact_text


class CandidateSource(StrEnum):
    SCENE = "scene"
    INTERVAL = "interval"


class FrameCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: float = Field(ge=0)
    path: Path
    sources: tuple[CandidateSource, ...]
    scene_score: float | None = Field(default=None, ge=0, le=1)
    perceptual_hash: str | None = None
    text_region_hash: str | None = None


class KeyframeExtractionError(RuntimeError):
    """Raised when bounded FFmpeg frame extraction fails."""


def analyze_frame(candidate: FrameCandidate) -> FrameCandidate:
    if not candidate.path.is_file() or candidate.path.is_symlink():
        raise ValueError("keyframe candidate must be a regular image")
    with Image.open(candidate.path) as image:
        rgb = image.convert("RGB")
        full_hash = str(imagehash.phash(rgb))
        width, height = rgb.size
        text_region = rgb.crop((0, int(height * 0.55), width, height))
        # Captions often occupy only a few pixels of an otherwise static frame.
        # A larger hash preserves enough spatial detail to notice that text changed.
        region_hash = str(imagehash.phash(text_region, hash_size=16))
    return candidate.model_copy(
        update={"perceptual_hash": full_hash, "text_region_hash": region_hash}
    )


def _distance(first: str | None, second: str | None) -> int:
    if first is None or second is None:
        return 64
    return int(imagehash.hex_to_hash(first) - imagehash.hex_to_hash(second))


def select_keyframes(
    candidates: tuple[FrameCandidate, ...],
    *,
    duplicate_distance: int = 4,
    text_change_distance: int = 4,
) -> tuple[FrameCandidate, ...]:
    """Remove near-duplicates unless the likely caption region changed."""
    selected: list[FrameCandidate] = []
    for candidate in sorted(candidates, key=lambda frame: (frame.timestamp, str(frame.path))):
        analyzed = candidate if candidate.perceptual_hash else analyze_frame(candidate)
        if not selected:
            selected.append(analyzed)
            continue
        previous = selected[-1]
        if CandidateSource.SCENE in analyzed.sources:
            selected.append(analyzed)
            continue
        full_distance = _distance(previous.perceptual_hash, analyzed.perceptual_hash)
        text_distance = _distance(previous.text_region_hash, analyzed.text_region_hash)
        if full_distance <= duplicate_distance and text_distance < text_change_distance:
            continue
        selected.append(analyzed)
    return tuple(selected)


def _run_ffmpeg(command: list[str], *, timeout_seconds: float) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return completed.stderr
    except subprocess.TimeoutExpired as error:
        raise KeyframeExtractionError("FFmpeg keyframe extraction timed out") from error
    except subprocess.CalledProcessError as error:
        summary = redact_text((error.stderr or "FFmpeg rejected the video")[-500:])
        raise KeyframeExtractionError(f"FFmpeg keyframe extraction failed: {summary}") from error
    except OSError as error:
        raise KeyframeExtractionError("FFmpeg could not be executed") from error


def extract_video_candidates(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    scene_threshold: float = 0.35,
    maximum_interval_seconds: float = 5,
    timeout_seconds: float = 300,
    ffmpeg_executable: str = "ffmpeg",
) -> tuple[FrameCandidate, ...]:
    """Extract low-frequency interval frames plus independent scene-change frames."""
    if not 0 < scene_threshold <= 1 or maximum_interval_seconds <= 0:
        raise ValueError("keyframe thresholds must be positive and bounded")
    source = Path(video_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    interval_pattern = destination / "interval-%06d.jpg"
    scene_pattern = destination / "scene-%06d.jpg"
    _run_ffmpeg(
        [
            ffmpeg_executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"fps=1/{maximum_interval_seconds}",
            str(interval_pattern),
        ],
        timeout_seconds=timeout_seconds,
    )
    scene_log = _run_ffmpeg(
        [
            ffmpeg_executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"select=gt(scene\\,{scene_threshold}),showinfo",
            "-fps_mode",
            "vfr",
            str(scene_pattern),
        ],
        timeout_seconds=timeout_seconds,
    )
    merged: dict[float, FrameCandidate] = {}
    for index, path in enumerate(sorted(destination.glob("interval-*.jpg"))):
        timestamp = round(index * maximum_interval_seconds, 3)
        merged[timestamp] = FrameCandidate(
            timestamp=timestamp,
            path=path,
            sources=(CandidateSource.INTERVAL,),
        )
    scene_times = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", scene_log)]
    for index, path in enumerate(sorted(destination.glob("scene-*.jpg"))):
        timestamp = round(scene_times[index] if index < len(scene_times) else 0.0, 3)
        existing = merged.get(timestamp)
        sources = (
            (CandidateSource.SCENE, *existing.sources)
            if existing is not None
            else (CandidateSource.SCENE,)
        )
        merged[timestamp] = FrameCandidate(
            timestamp=timestamp,
            path=path,
            sources=tuple(dict.fromkeys(sources)),
            scene_score=scene_threshold,
        )
    analyzed = tuple(analyze_frame(candidate) for candidate in merged.values())
    return select_keyframes(analyzed)
