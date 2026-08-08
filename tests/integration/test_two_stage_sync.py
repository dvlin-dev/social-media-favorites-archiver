from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from social_media_favorites_archiver.models import TextSegment, TextSource
from social_media_favorites_archiver.orchestrator import SyncOptions, SyncOrchestrator
from social_media_favorites_archiver.queue import JobStatus, ProcessingStage
from social_media_favorites_archiver.safety.cleanup import BarrierState, DerivativeBarrier
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.markdown import (
    MarkdownRenderer,
    NoteRenderContext,
    parse_note,
)
from tests.integration.orchestrator_fakes import NOW, FixtureAdapter, collection, item


def _orchestrator(tmp_path: Path) -> SyncOrchestrator:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    return SyncOrchestrator(
        database,
        MarkdownRenderer(tmp_path / "vault"),
        now=lambda: NOW,
    )


def _barrier(state: BarrierState) -> DerivativeBarrier:
    return DerivativeBarrier.model_validate(
        {name: state for name in DerivativeBarrier.model_fields}
    )


def test_enumeration_renders_every_skeleton_before_heavy_work(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    adapter = FixtureAdapter(("BV1a", "BV1b", "BV1c"), page_size=2)

    result = asyncio.run(
        orchestrator.enumerate_collection(
            adapter,
            collection(),
            options=SyncOptions(force_full_sync=True),
        )
    )

    notes = sorted((tmp_path / "vault").rglob("*.md"))
    assert result.enumeration_complete is True
    assert result.skeletons_rendered == 3
    assert len(notes) == 3
    assert all(parse_note(path.read_text())[0]["processing_status"] == "queued" for path in notes)
    with orchestrator.database.connect() as connection:
        jobs = connection.execute("SELECT status FROM jobs ORDER BY id").fetchall()
    assert len(jobs) == 3
    assert {row["status"] for row in jobs} == {JobStatus.PENDING.value}


def test_heavy_completion_updates_same_note_and_restart_resumes_without_duplicates(
    tmp_path: Path,
) -> None:
    first = _orchestrator(tmp_path)
    adapter = FixtureAdapter(("BV1a", "BV1b"))
    asyncio.run(
        first.enumerate_collection(
            adapter,
            collection(),
            options=SyncOptions(force_full_sync=True),
        )
    )
    before = {parse_note(path.read_text())[0]["smfa_id"]: path for path in (tmp_path / "vault").rglob("*.md")}
    leased = first.queue.lease_next("worker-a", now=NOW)
    assert leased is not None
    completed_item = item("BV1a") if leased.item_id == first.item_id("bilibili:BV1a") else item("BV1b")
    context = NoteRenderContext(
        processing_status="complete",
        first_synced_at=NOW,
        last_synced_at=NOW + timedelta(minutes=1),
        transcript=(
            TextSegment(
                segment_id="asr-1",
                start_time=0,
                end_time=1,
                text="Heavy work finished",
                source=TextSource.ASR,
            ),
        ),
    )

    rendered = first.complete_heavy_job(
        leased,
        worker_id="worker-a",
        item=completed_item,
        context=context,
        barrier=_barrier(BarrierState.PENDING),
    )

    assert rendered.path == before[completed_item.canonical_id]
    assert "Heavy work finished" in rendered.path.read_text()
    assert first.queue.get(leased.id).status == JobStatus.SUCCEEDED

    restarted = _orchestrator(tmp_path)
    resumed = restarted.queue.lease_next("worker-b", now=NOW)
    assert resumed is not None
    assert resumed.id != leased.id
    with restarted.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2

    asyncio.run(
        restarted.enumerate_collection(
            FixtureAdapter(("BV1a", "BV1b")),
            collection(),
            options=SyncOptions(force_full_sync=True),
        )
    )
    with restarted.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_cleanup_is_scheduled_only_after_the_full_derivative_barrier(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    item_id = orchestrator.database.upsert_item(item("BV1a"))

    blocked = orchestrator.schedule_cleanup(
        item_id,
        item("BV1a").platform,
        item("BV1a").canonical_id,
        barrier=_barrier(BarrierState.SUCCEEDED).model_copy(
            update={"final_verification": BarrierState.FAILED}
        ),
    )
    ready = orchestrator.schedule_cleanup(
        item_id,
        item("BV1a").platform,
        item("BV1a").canonical_id,
        barrier=_barrier(BarrierState.SUCCEEDED),
    )

    assert blocked is None
    assert ready is not None
    assert ready.stage == ProcessingStage.CLEANUP
