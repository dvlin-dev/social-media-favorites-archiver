"""Local RapidOCR with ordered, auditable text blocks."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from social_media_favorites_archiver.models import Asset, AssetKind
from social_media_favorites_archiver.processors.terminology import (
    TerminologyCorrection,
    TerminologyCorrector,
)


class OCREngine(Protocol):
    def __call__(self, image_path: str) -> tuple[object, object]: ...


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True)

    points: tuple[tuple[float, float], ...]

    @field_validator("points")
    @classmethod
    def validate_points(
        cls,
        value: tuple[tuple[float, float], ...],
    ) -> tuple[tuple[float, float], ...]:
        if len(value) != 4:
            msg = "OCR bounding boxes require four points"
            raise ValueError(msg)
        return value


class OCRBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    block_id: str
    asset_id: str
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    raw_text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    bounding_box: BoundingBox
    timestamp: float | None = Field(default=None, ge=0)
    provenance: tuple[str, ...]
    corrections: tuple[TerminologyCorrection, ...] = ()


class OCRResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    backend: str
    model: str
    quality: str | None = None
    blocks: tuple[OCRBlock, ...] = ()
    verified_no_text: bool


class OCRBackendUnavailable(RuntimeError):
    """Raised when the configured local OCR runtime is absent."""


class RapidOCRBackend:
    name = "rapidocr"
    model = "rapidocr-onnxruntime-default"

    def __init__(
        self,
        *,
        engine: OCREngine | None = None,
        terminology: dict[str, str] | None = None,
    ) -> None:
        if engine is None:
            if importlib.util.find_spec("rapidocr_onnxruntime") is None:
                raise OCRBackendUnavailable(
                    "RapidOCR is not installed; run `uv sync --extra ocr`."
                )
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

            engine = RapidOCR()
        self.engine = engine
        self.corrector = TerminologyCorrector(terminology)

    def recognize(
        self,
        image_path: str | Path,
        *,
        asset_id: str,
        timestamp: float | None = None,
        quality: str | None = None,
    ) -> OCRResult:
        path = Path(image_path)
        if not path.is_file() or path.is_symlink():
            raise ValueError("OCR input must be a regular local image")
        raw_result, _ = self.engine(str(path))
        blocks: list[OCRBlock] = []
        if isinstance(raw_result, list):
            for ordinal, raw_block in enumerate(raw_result):
                if not isinstance(raw_block, (list, tuple)) or len(raw_block) < 3:
                    continue
                raw_box, raw_text, raw_confidence = raw_block[:3]
                if not isinstance(raw_text, str) or not raw_text.strip():
                    continue
                if not isinstance(raw_confidence, (int, float)):
                    continue
                points = self._points(raw_box)
                correction = self.corrector.apply(raw_text.strip())
                identity = hashlib.sha256(
                    f"{asset_id}:{ordinal}:{correction.raw_text}:{points}".encode()
                ).hexdigest()[:20]
                blocks.append(
                    OCRBlock(
                        block_id=f"ocr-{identity}",
                        asset_id=asset_id,
                        ordinal=ordinal,
                        text=correction.corrected_text,
                        raw_text=correction.raw_text,
                        confidence=float(raw_confidence),
                        bounding_box=BoundingBox(points=points),
                        timestamp=timestamp,
                        provenance=(self.name, self.model),
                        corrections=correction.corrections,
                    )
                )
        return OCRResult(
            asset_id=asset_id,
            backend=self.name,
            model=self.model,
            quality=quality,
            blocks=tuple(blocks),
            verified_no_text=not blocks,
        )

    @staticmethod
    def _points(value: object) -> tuple[tuple[float, float], ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("RapidOCR returned an invalid bounding box")
        points: list[tuple[float, float]] = []
        for point in value:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("RapidOCR returned an invalid bounding-box point")
            x, y = point
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                raise ValueError("RapidOCR returned non-numeric coordinates")
            points.append((float(x), float(y)))
        return tuple(points)


def process_ordered_images(
    assets: tuple[Asset, ...],
    backend: RapidOCRBackend,
) -> tuple[OCRResult, ...]:
    """OCR image assets in platform order and keep every result attached to its image."""
    ordinals = [asset.ordinal for asset in assets]
    if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)):
        raise ValueError("image assets must have unique ascending ordinals")
    results: list[OCRResult] = []
    for asset in assets:
        if asset.kind != AssetKind.IMAGE or asset.local_path is None:
            raise ValueError("ordered OCR requires downloaded image assets")
        results.append(
            backend.recognize(
                asset.local_path,
                asset_id=asset.asset_id,
                quality=asset.quality,
            )
        )
    return tuple(results)
