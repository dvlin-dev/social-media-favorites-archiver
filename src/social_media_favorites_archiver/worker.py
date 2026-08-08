"""Durable local heavy-stage worker used by foreground CLI draining."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from social_media_favorites_archiver.adapters.base import (
    AdapterError,
    AdapterErrorCode,
    BaseAdapter,
)
from social_media_favorites_archiver.config import (
    AppSettings,
    ASRBackend,
    CleanupPolicy,
    OCRBackend,
    select_asr_backend,
)
from social_media_favorites_archiver.models import (
    AssetKind,
    NormalizedItem,
    Platform,
    SourceAvailability,
    TextSegment,
    TextSource,
)
from social_media_favorites_archiver.orchestrator import SyncOrchestrator
from social_media_favorites_archiver.processors.asr import (
    ASRRequest,
    FasterWhisperBackend,
    FunASRBackend,
    LocalASRBackend,
    MlxWhisperBackend,
    WhisperCppBackend,
    backend_availability,
    extract_audio,
)
from social_media_favorites_archiver.processors.enrichment import (
    EnrichmentInput,
    EnrichmentStatus,
    OpenAICompatibleEnricher,
)
from social_media_favorites_archiver.processors.fusion import FusionKind, fuse_timelines
from social_media_favorites_archiver.processors.keyframes import extract_video_candidates
from social_media_favorites_archiver.processors.ocr import RapidOCRBackend, process_ordered_images
from social_media_favorites_archiver.queue import JobQueue, JobRecord, ProcessingStage
from social_media_favorites_archiver.safety.cleanup import (
    BarrierState,
    CleanupManager,
    DerivativeBarrier,
)
from social_media_favorites_archiver.safety.paths import AssetPathManager
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.markdown import (
    GENERATED_END,
    GENERATED_START,
    NoteRenderContext,
    parse_note,
)

WORKER_VERSION = "local-worker-v1"
EXTRACTION_TYPE = "worker_text_v1"
FUSION_TYPE = "worker_fusion_v1"


class WorkerRetryableError(RuntimeError):
    """A sanitized heavy-stage failure that should remain retryable."""


class WorkerNeedsAuthError(RuntimeError):
    """The platform session must be repaired before retrying."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractionBundle(_FrozenModel):
    spoken: tuple[TextSegment, ...] = ()
    visual: tuple[TextSegment, ...] = ()
    image_ocr: dict[str, tuple[str, ...]] = {}


class FusionBundle(_FrozenModel):
    transcript: tuple[TextSegment, ...] = ()
    image_ocr: dict[str, tuple[str, ...]] = {}


@dataclass(frozen=True)
class WorkerResources:
    settings: AppSettings
    database: Database
    queue: JobQueue
    orchestrator: SyncOrchestrator
    adapters: Mapping[Platform, BaseAdapter]


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _input_fingerprint(item: NormalizedItem) -> str:
    return _hash(
        {
            "canonical_id": item.canonical_id,
            "metadata_fingerprint": item.metadata_fingerprint,
            "assets": [
                {
                    "asset_id": asset.asset_id,
                    "kind": asset.kind.value,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                }
                for asset in item.assets
            ],
        }
    )


def _config_hash(settings: AppSettings) -> str:
    return _hash(
        {
            "asr_backend": settings.asr_backend.value,
            "asr_model": settings.asr_model,
            "ocr_backend": settings.ocr_backend.value,
            "terminology": str(settings.terminology_dictionary or ""),
            "worker": WORKER_VERSION,
        }
    )


def _terminology(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        original, replacement = stripped.split("=", 1)
        if original.strip() and replacement.strip():
            result[original.strip()] = replacement.strip()
    return result


def _asr_backend(settings: AppSettings) -> LocalASRBackend:
    candidates = {
        backend
        for backend in (
            ASRBackend.FUNASR,
            ASRBackend.MLX_WHISPER,
            ASRBackend.WHISPER_CPP,
            ASRBackend.FASTER_WHISPER,
        )
        if backend_availability(backend).available
    }
    selected = select_asr_backend(settings, available=candidates)
    if selected == ASRBackend.FUNASR:
        return FunASRBackend(model=settings.asr_model)
    if selected == ASRBackend.MLX_WHISPER:
        return MlxWhisperBackend(model=settings.asr_model)
    if selected == ASRBackend.FASTER_WHISPER:
        return FasterWhisperBackend(model=settings.asr_model)
    if selected == ASRBackend.WHISPER_CPP:
        return WhisperCppBackend(model=settings.asr_model)
    raise WorkerRetryableError("no configured local ASR backend is available")


def _ready_barrier() -> DerivativeBarrier:
    return DerivativeBarrier.model_validate(
        {name: BarrierState.SUCCEEDED for name in DerivativeBarrier.model_fields}
    )


class LocalHeavyWorker:
    """Advance one leased job and enqueue its next durable stage."""

    _NEXT: ClassVar[dict[ProcessingStage, ProcessingStage]] = {
        ProcessingStage.ASSETS: ProcessingStage.EXTRACTION,
        ProcessingStage.EXTRACTION: ProcessingStage.FUSION,
        ProcessingStage.FUSION: ProcessingStage.ENRICHMENT,
        ProcessingStage.ENRICHMENT: ProcessingStage.RENDER,
        ProcessingStage.RENDER: ProcessingStage.VERIFY,
        ProcessingStage.VERIFY: ProcessingStage.CLEANUP,
    }

    def __init__(self, resources: WorkerResources) -> None:
        self.resources = resources

    def _item(self, job: JobRecord) -> NormalizedItem:
        if job.item_id is None:
            raise WorkerRetryableError("heavy job has no item identity")
        return self.resources.database.get_item(job.item_id)

    def _advance(self, job: JobRecord, worker_id: str, item: NormalizedItem) -> None:
        next_stage = self._NEXT.get(job.stage)
        if next_stage is not None:
            self.resources.queue.enqueue(
                item_id=job.item_id,
                platform=item.platform,
                stage=next_stage,
                idempotency_key=(
                    f"{item.canonical_id}:worker:{next_stage.value}:"
                    f"{item.metadata_fingerprint}:{WORKER_VERSION}"
                ),
            )
        self.resources.queue.complete(job.id, worker_id)

    async def process(self, job: JobRecord, worker_id: str) -> None:
        item = self._item(job)
        if job.stage == ProcessingStage.ASSETS:
            await self._assets(job, worker_id, item)
        elif job.stage == ProcessingStage.EXTRACTION:
            await self._extract(job, worker_id, item)
        elif job.stage == ProcessingStage.FUSION:
            self._fuse(job, worker_id, item)
        elif job.stage == ProcessingStage.ENRICHMENT:
            self._enrich(job, worker_id, item)
        elif job.stage == ProcessingStage.RENDER:
            self._render(job, worker_id, item)
        elif job.stage == ProcessingStage.VERIFY:
            self._verify(job, worker_id, item)
        elif job.stage == ProcessingStage.CLEANUP:
            self._cleanup(job, worker_id, item)
        else:
            raise WorkerRetryableError("foreground worker received an unsupported stage")

    @staticmethod
    def _assets_ready(item: NormalizedItem) -> bool:
        if item.source_availability == SourceAvailability.UNAVAILABLE:
            return True
        if item.platform.value == "bilibili" and item.native_subtitles:
            return True
        local = [
            asset
            for asset in item.assets
            if asset.local_path is not None
            and asset.sha256 is not None
            and asset.local_path.is_file()
            and not asset.local_path.is_symlink()
        ]
        if item.platform.value == "bilibili":
            return any(asset.kind in {AssetKind.VIDEO, AssetKind.AUDIO} for asset in local)
        required = [asset for asset in item.assets if asset.source_url is not None]
        return not required or len(local) >= len(required)

    async def _assets(
        self,
        job: JobRecord,
        worker_id: str,
        item: NormalizedItem,
    ) -> None:
        adapter = self.resources.adapters.get(item.platform)
        if adapter is None:
            raise WorkerRetryableError("platform adapter is unavailable")
        if not self._assets_ready(item):
            directory = AssetPathManager(self.resources.settings.cache_path).item_directory(
                item.canonical_id
            )
            try:
                downloaded = await adapter.download_assets(item, directory)
            except AdapterError as error:
                if error.code in {
                    AdapterErrorCode.NEEDS_AUTH,
                    AdapterErrorCode.NEEDS_USER_ACTION,
                }:
                    raise WorkerNeedsAuthError("platform authentication is required") from error
                raise WorkerRetryableError("asset download failed") from error
            used_ordinals = {asset.ordinal for asset in item.assets}
            existing_ids = {asset.asset_id for asset in item.assets}
            next_ordinal = max(used_ordinals, default=-1) + 1
            for asset in downloaded:
                adjusted = asset
                if asset.asset_id not in existing_ids and asset.ordinal in used_ordinals:
                    adjusted = asset.model_copy(update={"ordinal": next_ordinal})
                    next_ordinal += 1
                self.resources.database.upsert_asset(job.item_id or 0, adjusted)
                used_ordinals.add(adjusted.ordinal)
                existing_ids.add(adjusted.asset_id)
            item = self._item(job)
        self._advance(job, worker_id, item)

    async def _extract(
        self,
        job: JobRecord,
        worker_id: str,
        item: NormalizedItem,
    ) -> None:
        spoken = list(item.native_subtitles)
        visual: list[TextSegment] = []
        image_ocr: dict[str, tuple[str, ...]] = {}
        local_media = [
            asset
            for asset in item.assets
            if asset.local_path is not None
            and asset.local_path.is_file()
            and asset.kind in {AssetKind.VIDEO, AssetKind.AUDIO}
        ]
        requires_asr = bool(item.platform_metadata.get("requires_local_asr"))
        if requires_asr and not spoken:
            if not local_media:
                raise WorkerRetryableError("local ASR requires a downloaded media asset")
            media = local_media[0]
            assert media.local_path is not None
            audio_path = (
                AssetPathManager(self.resources.settings.cache_path).item_directory(
                    item.canonical_id
                )
                / "derivatives"
                / "audio.wav"
            )
            extract_audio(media.local_path, audio_path, timeout_seconds=600)
            asr_result = _asr_backend(self.resources.settings).transcribe(
                audio_path,
                ASRRequest(terminology=_terminology(self.resources.settings.terminology_dictionary)),
            )
            spoken.extend(asr_result.segments)

        local_images = tuple(
            asset
            for asset in item.assets
            if asset.kind == AssetKind.IMAGE
            and asset.local_path is not None
            and asset.local_path.is_file()
        )
        should_frame_ocr = bool(
            item.platform_metadata.get("requires_adaptive_frame_ocr")
        )
        if self.resources.settings.ocr_backend == OCRBackend.RAPIDOCR and (
            local_images or (should_frame_ocr and local_media)
        ):
            ocr = RapidOCRBackend(terminology=_terminology(self.resources.settings.terminology_dictionary))
            if local_images:
                for ocr_result in process_ordered_images(local_images, ocr):
                    texts = tuple(block.text for block in ocr_result.blocks)
                    image_ocr[ocr_result.asset_id] = texts
                    for block in ocr_result.blocks:
                        visual.append(
                            TextSegment(
                                segment_id=block.block_id,
                                start_time=0,
                                end_time=0,
                                text=block.text,
                                raw_text=block.raw_text,
                                source=TextSource.OCR,
                                confidence=block.confidence,
                                asset_id=block.asset_id,
                                provenance=block.provenance,
                            )
                        )
            if should_frame_ocr and local_media:
                video = next(
                    (asset for asset in local_media if asset.kind == AssetKind.VIDEO),
                    None,
                )
                if video is not None and video.local_path is not None:
                    frame_dir = (
                        AssetPathManager(self.resources.settings.cache_path).item_directory(
                            item.canonical_id
                        )
                        / "derivatives"
                        / "frames"
                    )
                    for index, frame in enumerate(
                        extract_video_candidates(video.local_path, frame_dir)
                    ):
                        ocr_result = ocr.recognize(
                            frame.path,
                            asset_id=f"{video.asset_id}:frame:{index}",
                            timestamp=frame.timestamp,
                        )
                        for block in ocr_result.blocks:
                            timestamp = block.timestamp or 0
                            visual.append(
                                TextSegment(
                                    segment_id=block.block_id,
                                    start_time=timestamp,
                                    end_time=timestamp,
                                    text=block.text,
                                    raw_text=block.raw_text,
                                    source=TextSource.BURNED_CAPTION,
                                    confidence=block.confidence,
                                    asset_id=block.asset_id,
                                    provenance=block.provenance,
                                )
                            )
        bundle = ExtractionBundle(
            spoken=tuple(spoken),
            visual=tuple(visual),
            image_ocr=image_ocr,
        )
        self.resources.database.upsert_extraction(
            job.item_id or 0,
            extraction_type=EXTRACTION_TYPE,
            processor_version=WORKER_VERSION,
            input_fingerprint=_input_fingerprint(item),
            config_hash=_config_hash(self.resources.settings),
            payload=bundle.model_dump(mode="json"),
        )
        self._advance(job, worker_id, item)

    def _extraction(self, job: JobRecord) -> ExtractionBundle:
        payload = self.resources.database.latest_extraction(job.item_id or 0, EXTRACTION_TYPE)
        if payload is None:
            raise WorkerRetryableError("text extraction result is missing")
        return ExtractionBundle.model_validate(payload)

    def _fusion(self, job: JobRecord) -> FusionBundle:
        payload = self.resources.database.latest_extraction(job.item_id or 0, FUSION_TYPE)
        if payload is None:
            raise WorkerRetryableError("timeline fusion result is missing")
        return FusionBundle.model_validate(payload)

    def _fuse(self, job: JobRecord, worker_id: str, item: NormalizedItem) -> None:
        extraction = self._extraction(job)
        result = fuse_timelines((*extraction.spoken, *extraction.visual))
        transcript: list[TextSegment] = []
        for segment in result.segments:
            native = any(
                reading.source == TextSource.NATIVE_SUBTITLE for reading in segment.readings
            )
            source = (
                TextSource.NATIVE_SUBTITLE
                if native
                else TextSource.ASR
                if segment.kind in {FusionKind.SPOKEN, FusionKind.CONFLICT}
                else TextSource.VISUAL_ANNOTATION
            )
            transcript.append(
                TextSegment(
                    segment_id=segment.segment_id,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    text=segment.text,
                    source=source,
                    confidence=segment.confidence,
                    provenance=segment.provenance,
                )
            )
        bundle = FusionBundle(
            transcript=tuple(transcript),
            image_ocr=extraction.image_ocr,
        )
        self.resources.database.upsert_extraction(
            job.item_id or 0,
            extraction_type=FUSION_TYPE,
            processor_version=WORKER_VERSION,
            input_fingerprint=_input_fingerprint(item),
            config_hash=_config_hash(self.resources.settings),
            payload=bundle.model_dump(mode="json"),
        )
        self._advance(job, worker_id, item)

    def _enrich(self, job: JobRecord, worker_id: str, item: NormalizedItem) -> None:
        fusion = self._fusion(job)
        extraction = self._extraction(job)
        outcome = OpenAICompatibleEnricher(
            enabled=self.resources.settings.enrichment_enabled
        ).enrich(
            EnrichmentInput(
                title=item.title,
                author=item.author,
                platform=item.platform,
                original_text=item.original_text,
                transcript="\n".join(segment.text for segment in fusion.transcript) or None,
                ocr_text="\n".join(segment.text for segment in extraction.visual) or None,
            )
        )
        if outcome.status == EnrichmentStatus.RETRYABLE_FAILURE:
            raise WorkerRetryableError("optional enrichment should be retried")
        if outcome.status == EnrichmentStatus.SUCCEEDED:
            self.resources.database.upsert_enrichment(job.item_id or 0, outcome)
        self._advance(job, worker_id, item)

    def _render(self, job: JobRecord, worker_id: str, item: NormalizedItem) -> None:
        fusion = self._fusion(job)
        stored = self.resources.database.latest_enrichment(job.item_id or 0) or {}
        result = stored.get("result")
        enrichment = result if isinstance(result, dict) else {}
        rendered = self.resources.orchestrator.renderer.render(
            item,
            NoteRenderContext(
                collections=self.resources.database.active_collection_names(job.item_id or 0),
                favorite_state=self.resources.database.derived_favorite_state(job.item_id or 0),
                processing_status="complete",
                first_synced_at=item.first_seen_at,
                last_synced_at=item.last_seen_at,
                summary=(
                    str(enrichment["summary"])
                    if isinstance(enrichment.get("summary"), str)
                    else None
                ),
                key_points=tuple(
                    str(value)
                    for value in enrichment.get("key_points", ())
                    if isinstance(value, str)
                ),
                generated_tags=tuple(
                    str(value)
                    for value in enrichment.get("tags", ())
                    if isinstance(value, str)
                ),
                topics=tuple(
                    str(value)
                    for value in enrichment.get("topics", ())
                    if isinstance(value, str)
                ),
                transcript=fusion.transcript,
                image_ocr=fusion.image_ocr,
            ),
        )
        if rendered.status == "conflict":
            raise WorkerRetryableError(rendered.diagnostic_code or "note conflict")
        self._advance(job, worker_id, item)

    def _verify(self, job: JobRecord, worker_id: str, item: NormalizedItem) -> None:
        matches: list[Path] = []
        for path in self.resources.settings.vault_path.rglob("*.md"):
            try:
                frontmatter, body = parse_note(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if frontmatter.get("smfa_id") == item.canonical_id:
                if body.count(GENERATED_START) != 1 or body.count(GENERATED_END) != 1:
                    raise WorkerRetryableError("rendered note markers could not be verified")
                matches.append(path)
        if len(matches) != 1:
            raise WorkerRetryableError("rendered note identity could not be verified")
        self._advance(job, worker_id, item)

    def _cleanup(self, job: JobRecord, worker_id: str, item: NormalizedItem) -> None:
        if self.resources.settings.cleanup_policy == CleanupPolicy.AFTER_VERIFIED:
            manager = CleanupManager(
                self.resources.database,
                AssetPathManager(self.resources.settings.cache_path),
            )
            for asset in item.assets:
                if (
                    asset.kind in {AssetKind.VIDEO, AssetKind.AUDIO}
                    and asset.local_path is not None
                    and asset.local_path.exists()
                ):
                    manager.cleanup(
                        item.canonical_id,
                        asset.local_path,
                        barrier=_ready_barrier(),
                    )
        self.resources.queue.complete(job.id, worker_id)
