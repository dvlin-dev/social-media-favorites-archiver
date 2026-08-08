"""Two-stage enumeration, durable heavy jobs, and safe collection reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from social_media_favorites_archiver.adapters.base import BaseAdapter
from social_media_favorites_archiver.models import (
    Collection,
    MembershipState,
    NormalizedItem,
    Platform,
)
from social_media_favorites_archiver.queue import (
    JobQueue,
    JobRecord,
    JobStatus,
    ProcessingStage,
)
from social_media_favorites_archiver.safety.cleanup import DerivativeBarrier
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.markdown import (
    MarkdownRenderer,
    NoteRenderContext,
    RenderResult,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("orchestrator timestamps must include timezone information")
    return value.astimezone(UTC).isoformat()


class SyncOptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    early_stop_threshold: int = Field(default=20, ge=1)
    force_full_sync: bool = False
    item_limit: int | None = Field(default=None, ge=1)


class SyncResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    references_observed: int = Field(ge=0)
    items_fetched: int = Field(ge=0)
    unchanged_items: int = Field(ge=0)
    refavorited: int = Field(ge=0)
    skeletons_rendered: int = Field(ge=0)
    jobs_enqueued: int = Field(ge=0)
    memberships_removed: int = Field(ge=0)
    early_stopped: bool
    limited: bool = False
    enumeration_complete: bool


class ProcessorVersions(BaseModel):
    model_config = ConfigDict(frozen=True)

    asr: str = Field(min_length=1)
    ocr: str = Field(min_length=1)
    fusion: str = Field(min_length=1)


def asset_job_key(canonical_id: str, source_fingerprint: str) -> str:
    """Key downloads only by cheap source state, never by processor versions."""
    return f"{canonical_id}:assets:{source_fingerprint}"


def derivation_job_key(
    canonical_id: str,
    asset_hashes: tuple[str, ...],
    versions: ProcessorVersions,
) -> str:
    """Key derivations by immutable assets plus local processor versions."""
    payload = json.dumps(
        {
            "asset_hashes": sorted(asset_hashes),
            "versions": versions.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"{canonical_id}:derivations:sha256:{digest}"


class SyncOrchestrator:
    """Render metadata first, then leave all expensive work in SQLite jobs."""

    def __init__(
        self,
        database: Database,
        renderer: MarkdownRenderer,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.database = database
        self.renderer = renderer
        self.queue = JobQueue(database)
        self._now = now

    def item_id(self, canonical_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM items WHERE canonical_id = ?", (canonical_id,)
            ).fetchone()
        if row is None:
            raise KeyError(canonical_id)
        return int(row[0])

    def collection_id(self, canonical_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM collections WHERE canonical_id = ?", (canonical_id,)
            ).fetchone()
        if row is None:
            raise KeyError(canonical_id)
        return int(row[0])

    def _start_run(self, platform: Platform, started_at: datetime) -> str:
        run_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO runs(id, platform, started_at, status) VALUES (?, ?, ?, ?)",
                (run_id, platform.value, _timestamp(started_at), "running"),
            )
        return run_id

    def _finish_run(
        self,
        run_id: str,
        *,
        status: str,
        complete: bool,
        stats: Mapping[str, object],
        finished_at: datetime,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, enumeration_complete = ?, stats_json = ?
                WHERE id = ?
                """,
                (
                    _timestamp(finished_at),
                    status,
                    int(complete),
                    json.dumps(stats, sort_keys=True, separators=(",", ":")),
                    run_id,
                ),
            )

    def _checkpoint_run(
        self,
        run_id: str,
        *,
        collection: Collection,
        cursor: str | None,
        references_observed: int,
        observed_at: datetime,
    ) -> None:
        """Persist a private local restart checkpoint without exposing it in reports."""
        stats = {
            "checkpoint_collection": collection.canonical_id,
            "checkpoint_cursor": cursor,
            "references_observed": references_observed,
        }
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE runs SET stats_json = ? WHERE id = ? AND status = 'running'",
                (json.dumps(stats, sort_keys=True, separators=(",", ":")), run_id),
            )

    def _known_state(self, canonical_id: str, collection_id: int) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT items.id, items.metadata_fingerprint, items.source_revision,
                       item_collections.state
                FROM items
                LEFT JOIN item_collections
                  ON item_collections.item_id = items.id
                 AND item_collections.collection_id = ?
                WHERE items.canonical_id = ?
                """,
                (collection_id, canonical_id),
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def _touch_known_item(
        self,
        item_id: int,
        collection_id: int,
        observed_at: datetime,
    ) -> None:
        self.database.set_membership(
            item_id,
            collection_id,
            MembershipState.ACTIVE,
            observed_at=observed_at,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE items SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                (_timestamp(observed_at), _timestamp(observed_at), item_id),
            )

    def _active_collection_names(self, item_id: int) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT collections.name
                FROM item_collections
                JOIN collections ON collections.id = item_collections.collection_id
                WHERE item_collections.item_id = ? AND item_collections.state = 'active'
                ORDER BY collections.canonical_id
                """,
                (item_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _render_skeleton(
        self,
        item: NormalizedItem,
        item_id: int,
        observed_at: datetime,
    ) -> RenderResult:
        return self.renderer.render(
            item,
            NoteRenderContext(
                collections=self._active_collection_names(item_id),
                favorite_state=MembershipState.ACTIVE,
                processing_status="queued",
                first_synced_at=item.first_seen_at,
                last_synced_at=observed_at,
                transcript=item.native_subtitles,
            ),
        )

    def _reconcile_complete_collection(
        self,
        collection_id: int,
        seen_item_ids: set[int],
        *,
        run_id: str,
        observed_at: datetime,
    ) -> int:
        placeholders = ",".join("?" for _ in seen_item_ids)
        unseen_clause = (
            f"AND item_id NOT IN ({placeholders})" if placeholders else ""
        )
        parameters: list[object] = [
            _timestamp(observed_at),
            run_id,
            collection_id,
            *sorted(seen_item_ids),
        ]
        with self.database.transaction(immediate=True) as connection:
            removed = connection.execute(
                f"""
                UPDATE item_collections
                SET state = 'removed', removed_at = ?, last_complete_run_id = ?
                WHERE collection_id = ? AND state = 'active' {unseen_clause}
                """,
                parameters,
            ).rowcount
            if seen_item_ids:
                connection.execute(
                    f"""
                    UPDATE item_collections
                    SET last_complete_run_id = ?
                    WHERE collection_id = ? AND item_id IN ({placeholders})
                    """,
                    (run_id, collection_id, *sorted(seen_item_ids)),
                )
        return int(removed)

    async def enumerate_collection(
        self,
        adapter: BaseAdapter,
        collection: Collection,
        *,
        options: SyncOptions | None = None,
    ) -> SyncResult:
        settings = options or SyncOptions()
        if adapter.platform != collection.platform:
            raise ValueError("adapter and collection platforms do not match")
        observed_at = self._now()
        run_id = self._start_run(adapter.platform, observed_at)
        collection_id = self.database.upsert_collection(collection)
        cursor: str | None = None
        seen_item_ids: set[int] = set()
        references_observed = 0
        items_fetched = 0
        unchanged_items = 0
        refavorited = 0
        skeletons_rendered = 0
        jobs_enqueued = 0
        consecutive_unchanged = 0
        early_stop_evidence = True
        reported_total: int | None = None
        early_stopped = False
        limited = False
        enumeration_complete = False

        try:
            while True:
                self._checkpoint_run(
                    run_id,
                    collection=collection,
                    cursor=cursor,
                    references_observed=references_observed,
                    observed_at=observed_at,
                )
                page = await adapter.list_favorites(collection, cursor)
                if cursor is not None and page.next_cursor == cursor:
                    raise ValueError("adapter repeated a pagination cursor")
                early_stop_evidence = (
                    early_stop_evidence
                    and page.ordering_stable
                    and page.total_count is not None
                )
                if page.total_count is not None:
                    if reported_total is None:
                        reported_total = page.total_count
                    elif reported_total != page.total_count:
                        early_stop_evidence = False
                for page_index, reference in enumerate(page.items):
                    if (
                        settings.item_limit is not None
                        and references_observed >= settings.item_limit
                    ):
                        limited = True
                        break
                    if reference.platform != adapter.platform:
                        raise ValueError("favorite reference platform does not match adapter")
                    references_observed += 1
                    known = self._known_state(reference.canonical_id, collection_id)
                    membership_state = None if known is None else known["state"]
                    was_refavorited = membership_state == MembershipState.REMOVED.value
                    unchanged = (
                        known is not None
                        and membership_state == MembershipState.ACTIVE.value
                        and reference.metadata_fingerprint is not None
                        and reference.metadata_fingerprint == known["metadata_fingerprint"]
                        and (
                            reference.source_revision is None
                            or reference.source_revision == known["source_revision"]
                        )
                    )
                    if unchanged:
                        assert known is not None
                        item_id = int(known["id"])
                        self._touch_known_item(item_id, collection_id, observed_at)
                        seen_item_ids.add(item_id)
                        unchanged_items += 1
                        consecutive_unchanged += 1
                    else:
                        consecutive_unchanged = 0
                        fetched = await adapter.fetch_item(reference)
                        if fetched.canonical_id != reference.canonical_id:
                            raise ValueError("fetched item identity does not match its reference")
                        collection_ids = tuple(
                            dict.fromkeys(
                                (*fetched.collection_canonical_ids, collection.canonical_id)
                            )
                        )
                        fetched = fetched.model_copy(
                            update={
                                "collection_canonical_ids": collection_ids,
                                "last_seen_at": max(fetched.last_seen_at, observed_at),
                            }
                        )
                        item_id = self.database.upsert_item(fetched)
                        for asset in fetched.assets:
                            self.database.upsert_asset(item_id, asset)
                        self.database.set_membership(
                            item_id,
                            collection_id,
                            MembershipState.ACTIVE,
                            observed_at=observed_at,
                        )
                        seen_item_ids.add(item_id)
                        render_result = self._render_skeleton(fetched, item_id, observed_at)
                        if render_result.status == "conflict":
                            raise RuntimeError(
                                render_result.diagnostic_code or "note skeleton conflict"
                            )
                        skeletons_rendered += 1
                        existing_jobs = self._job_count()
                        self.queue.enqueue(
                            item_id=item_id,
                            platform=fetched.platform,
                            stage=ProcessingStage.ASSETS,
                            idempotency_key=asset_job_key(
                                fetched.canonical_id, fetched.metadata_fingerprint
                            ),
                            now=observed_at,
                        )
                        jobs_enqueued += int(self._job_count() > existing_jobs)
                        items_fetched += 1
                        refavorited += int(was_refavorited)

                    if (
                        not settings.force_full_sync
                        and early_stop_evidence
                        and consecutive_unchanged >= settings.early_stop_threshold
                        and (page_index + 1 < len(page.items) or not page.complete)
                    ):
                        early_stopped = True
                        break
                    if (
                        settings.item_limit is not None
                        and references_observed >= settings.item_limit
                        and (page_index + 1 < len(page.items) or not page.complete)
                    ):
                        limited = True
                        break
                if limited:
                    break
                if early_stopped:
                    break
                if page.complete:
                    enumeration_complete = True
                    break
                cursor = page.next_cursor

            memberships_removed = 0
            if enumeration_complete:
                memberships_removed = self._reconcile_complete_collection(
                    collection_id,
                    seen_item_ids,
                    run_id=run_id,
                    observed_at=observed_at,
                )
            stats: dict[str, int | bool] = {
                "references_observed": references_observed,
                "items_fetched": items_fetched,
                "unchanged_items": unchanged_items,
                "refavorited": refavorited,
                "skeletons_rendered": skeletons_rendered,
                "jobs_enqueued": jobs_enqueued,
                "memberships_removed": memberships_removed,
                "early_stopped": early_stopped,
                "limited": limited,
            }
            self._finish_run(
                run_id,
                status=(
                    "succeeded"
                    if enumeration_complete
                    else "limited"
                    if limited
                    else "early_stopped"
                ),
                complete=enumeration_complete,
                stats=stats,
                finished_at=self._now(),
            )
            return SyncResult(
                run_id=run_id,
                references_observed=references_observed,
                items_fetched=items_fetched,
                unchanged_items=unchanged_items,
                refavorited=refavorited,
                skeletons_rendered=skeletons_rendered,
                jobs_enqueued=jobs_enqueued,
                memberships_removed=memberships_removed,
                early_stopped=early_stopped,
                limited=limited,
                enumeration_complete=enumeration_complete,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._finish_run(
                run_id,
                status="cancelled",
                complete=False,
                stats={
                    "references_observed": references_observed,
                    "checkpoint_collection": collection.canonical_id,
                    "checkpoint_cursor": cursor,
                },
                finished_at=self._now(),
            )
            raise
        except BaseException:
            self._finish_run(
                run_id,
                status="failed",
                complete=False,
                stats={"references_observed": references_observed},
                finished_at=self._now(),
            )
            raise

    def _job_count(self) -> int:
        with self.database.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def enqueue_derivations(
        self,
        item_id: int,
        platform: Platform,
        canonical_id: str,
        *,
        asset_hashes: tuple[str, ...],
        versions: ProcessorVersions,
    ) -> JobRecord:
        return self.queue.enqueue(
            item_id=item_id,
            platform=platform,
            stage=ProcessingStage.EXTRACTION,
            idempotency_key=derivation_job_key(canonical_id, asset_hashes, versions),
            now=self._now(),
        )

    def schedule_cleanup(
        self,
        item_id: int,
        platform: Platform,
        canonical_id: str,
        *,
        barrier: DerivativeBarrier,
    ) -> JobRecord | None:
        if not barrier.ready:
            return None
        return self.queue.enqueue(
            item_id=item_id,
            platform=platform,
            stage=ProcessingStage.CLEANUP,
            idempotency_key=f"{canonical_id}:cleanup:derivative-barrier-v1",
            now=self._now(),
        )

    def complete_heavy_job(
        self,
        job: JobRecord,
        *,
        worker_id: str,
        item: NormalizedItem,
        context: NoteRenderContext,
        barrier: DerivativeBarrier,
    ) -> RenderResult:
        if job.status != JobStatus.RUNNING or job.item_id is None:
            raise ValueError("heavy completion requires a leased item job")
        if self.item_id(item.canonical_id) != job.item_id:
            raise ValueError("heavy completion item does not match the leased job")
        item_id = self.database.upsert_item(item)
        for asset in item.assets:
            self.database.upsert_asset(item_id, asset)
        render_context = context.model_copy(
            update={
                "collections": self._active_collection_names(item_id),
                "favorite_state": self.database.derived_favorite_state(item_id),
            }
        )
        rendered = self.renderer.render(item, render_context)
        if rendered.status == "conflict":
            raise RuntimeError(rendered.diagnostic_code or "note render conflict")
        self.queue.complete(job.id, worker_id, now=self._now())
        self.schedule_cleanup(
            item_id,
            item.platform,
            item.canonical_id,
            barrier=barrier,
        )
        return rendered
