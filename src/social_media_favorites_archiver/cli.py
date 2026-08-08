"""Command-line interface for Social Media Favorites Archiver."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from social_media_favorites_archiver.adapters.base import BaseAdapter
from social_media_favorites_archiver.adapters.bilibili import (
    BilibiliAdapter,
    BilibiliPageDiscovery,
    YtDlpBridge,
)
from social_media_favorites_archiver.adapters.douyin import (
    DouyinAdapter,
    DouyinBrowserBridge,
)
from social_media_favorites_archiver.adapters.xiaohongshu import (
    XiaohongshuAdapter,
    XiaohongshuBrowserBridge,
)
from social_media_favorites_archiver.browser.interception import PageContextClient
from social_media_favorites_archiver.browser.session import BrowserSession
from social_media_favorites_archiver.config import AppSettings, load_settings
from social_media_favorites_archiver.diagnostics import run_doctor
from social_media_favorites_archiver.models import Collection, Platform
from social_media_favorites_archiver.orchestrator import (
    SyncOptions,
    SyncOrchestrator,
    SyncResult,
)
from social_media_favorites_archiver.queue import (
    InvalidJobTransition,
    JobQueue,
    JobRecord,
    JobStatus,
)
from social_media_favorites_archiver.reporting import build_run_report, build_status
from social_media_favorites_archiver.safety.cleanup import (
    BarrierState,
    CleanupManager,
    DerivativeBarrier,
)
from social_media_favorites_archiver.safety.paths import AssetPathManager
from social_media_favorites_archiver.storage.assets import AssetStore, CacheGuard
from social_media_favorites_archiver.storage.database import Database
from social_media_favorites_archiver.storage.markdown import MarkdownRenderer
from social_media_favorites_archiver.worker import (
    LocalHeavyWorker,
    WorkerNeedsAuthError,
    WorkerResources,
    WorkerRetryableError,
)


class ExitCode(IntEnum):
    OK = 0
    OPERATIONAL_ERROR = 1
    INVALID_USAGE = 2
    USER_ACTION_REQUIRED = 3
    INTERRUPTED = 130


app = typer.Typer(
    help="Archive your own social-media favorites into local Markdown.",
    no_args_is_help=True,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _emit(value: object, *, json_output: bool) -> None:
    if json_output:
        typer.echo(_json(value))
    elif isinstance(value, str):
        typer.echo(value)
    else:
        typer.echo(_json(value))


def _fail(message: str, *, code: ExitCode, json_output: bool = False) -> NoReturn:
    if json_output:
        typer.echo(_json({"error": message, "exit_code": int(code)}))
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=int(code))


def _settings(config: Path | None, *, json_output: bool) -> AppSettings:
    try:
        return load_settings(config)
    except (OSError, ValueError, ValidationError):
        _fail(
            "Configuration is invalid or unreadable; no values were displayed.",
            code=ExitCode.INVALID_USAGE,
            json_output=json_output,
        )


def _platforms(value: str, settings: AppSettings, *, json_output: bool) -> tuple[Platform, ...]:
    if value == "all":
        return settings.enabled_platforms
    try:
        platform = Platform(value)
    except ValueError:
        _fail(
            "Platform must be bilibili, xiaohongshu, douyin, or all.",
            code=ExitCode.INVALID_USAGE,
            json_output=json_output,
        )
    if platform not in settings.enabled_platforms:
        _fail(
            "The selected platform is disabled in configuration.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    return (platform,)


def _asset_store(settings: AppSettings) -> AssetStore:
    paths = AssetPathManager(settings.cache_path)
    return AssetStore(
        paths,
        max_asset_bytes=settings.cache_quota_bytes,
        allowed_mime_types={
            "image/jpeg",
            "image/png",
            "image/webp",
            "video/mp4",
            "video/webm",
            "audio/mp4",
            "audio/mpeg",
            "audio/wav",
        },
        cache_guard=CacheGuard(
            settings.cache_path,
            quota_bytes=settings.cache_quota_bytes,
            reserve_bytes=0,
        ),
    )


@asynccontextmanager
async def open_adapters(
    settings: AppSettings,
    platforms: tuple[Platform, ...],
) -> AsyncIterator[dict[Platform, BaseAdapter]]:
    """Attach one dedicated browser session and construct selected adapters."""
    session = BrowserSession(
        cdp_url=settings.browser_cdp_url,
        profile_path=settings.browser_profile_path,
    )
    page = await session.connect()
    client = PageContextClient(page)
    store = _asset_store(settings)
    adapters: dict[Platform, BaseAdapter] = {}
    if Platform.BILIBILI in platforms:
        adapters[Platform.BILIBILI] = BilibiliAdapter(
            bridge=YtDlpBridge(browser_profile=settings.browser_profile_path),
            discovery=BilibiliPageDiscovery(client),
        )
    if Platform.XIAOHONGSHU in platforms:
        adapters[Platform.XIAOHONGSHU] = XiaohongshuAdapter(
            bridge=XiaohongshuBrowserBridge(client),
            asset_store=store,
        )
    if Platform.DOUYIN in platforms:
        adapters[Platform.DOUYIN] = DouyinAdapter(
            bridge=DouyinBrowserBridge(client),
            asset_store=store,
        )
    try:
        yield adapters
    finally:
        await session.stop()


@dataclass(frozen=True)
class CommandResources:
    settings: AppSettings
    database: Database
    queue: JobQueue
    orchestrator: SyncOrchestrator
    adapters: dict[Platform, BaseAdapter]


async def process_foreground_job(
    job: JobRecord,
    worker_id: str,
    resources: CommandResources,
) -> None:
    """Run one durable local stage without sending media to cloud services."""
    worker = LocalHeavyWorker(
        WorkerResources(
            settings=resources.settings,
            database=resources.database,
            queue=resources.queue,
            orchestrator=resources.orchestrator,
            adapters=resources.adapters,
        )
    )
    await worker.process(job, worker_id)


async def _drain_foreground(resources: CommandResources) -> dict[str, int | bool]:
    worker_id = "smfa-foreground"
    drained = 0
    failures = 0
    current: JobRecord | None = None
    try:
        while True:
            current = resources.queue.lease_next(worker_id)
            if current is None:
                break
            try:
                await process_foreground_job(current, worker_id, resources)
            except (KeyboardInterrupt, asyncio.CancelledError):
                resources.queue.release_cancelled(current.id, worker_id)
                current = None
                raise
            except WorkerNeedsAuthError:
                resources.queue.mark_needs_auth(
                    current.id,
                    worker_id,
                    diagnostic_code="worker.needs_auth",
                )
                failures += 1
                current = None
                break
            except WorkerRetryableError:
                resources.queue.release_for_retry(
                    current.id,
                    worker_id,
                    diagnostic_code="worker.retryable",
                )
                failures += 1
                current = None
                break
            except Exception:
                resources.queue.release_for_retry(
                    current.id,
                    worker_id,
                    diagnostic_code="worker.processing_failed",
                )
                failures += 1
                current = None
                break
            updated = resources.queue.get(current.id)
            if updated.status == JobStatus.RUNNING:
                resources.queue.release_for_retry(
                    current.id,
                    worker_id,
                    diagnostic_code="worker.incomplete",
                )
                failures += 1
                current = None
                break
            if updated.status == JobStatus.SUCCEEDED:
                drained += 1
            else:
                failures += 1
            current = None
    finally:
        if current is not None:
            try:
                active = resources.queue.get(current.id)
                if active.status == JobStatus.RUNNING and active.lease_owner == worker_id:
                    resources.queue.release_cancelled(current.id, worker_id)
            except (KeyError, InvalidJobTransition, PermissionError):
                pass
    snapshot = build_status(resources.database)
    pending = sum(
        snapshot.jobs.get(status, 0)
        for status in ("pending", "retryable", "running")
    )
    return {
        "drain_requested": True,
        "drained": drained,
        "failures": failures,
        "pending": pending,
    }


async def _collection_list(
    adapter: BaseAdapter,
) -> tuple[Collection, ...]:
    cursor: str | None = None
    collections: list[Collection] = []
    while True:
        page = await adapter.list_collections(cursor)
        collections.extend(page.items)
        if page.complete:
            return tuple(collections)
        if page.next_cursor == cursor:
            raise ValueError("adapter repeated a collection cursor")
        cursor = page.next_cursor


def _select_collections(
    available: tuple[Collection, ...],
    filters: list[str] | None,
) -> tuple[Collection, ...]:
    if not filters:
        return available
    selected = tuple(
        entry
        for entry in available
        if entry.platform_collection_id in filters
        or entry.canonical_id in filters
        or entry.name in filters
    )
    return selected


@app.command()
def doctor(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a structured redacted report."),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Read settings from this YAML file."),
    ] = None,
) -> None:
    """Check local prerequisites without exposing secrets."""
    settings = _settings(config, json_output=json_output)
    report = run_doctor(settings)
    if json_output:
        _emit(report.model_dump(mode="json"), json_output=True)
    else:
        typer.echo(f"doctor: {report.status}")
        for check in report.checks:
            typer.echo(f"[{check.status}] {check.code}: {check.summary}")
        typer.echo("Optional enrichment variables (presence only):")
        for name, present in report.enrichment_presence.items():
            typer.echo(f"  {name}: {'present' if present else 'absent'}")
    if report.status == "fail":
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR))


@app.command()
def login(
    platform: Annotated[str, typer.Argument(help="Platform name or all.")] = "all",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Check or establish an authorized platform browser session."""
    settings = _settings(config, json_output=json_output)
    selected = _platforms(platform, settings, json_output=json_output)

    async def operation() -> tuple[list[dict[str, object]], bool]:
        rows: list[dict[str, object]] = []
        needs_action = False
        async with open_adapters(settings, selected) as adapters:
            for name in selected:
                adapter = adapters[name]
                state = await adapter.check_session()
                row: dict[str, object] = {
                    "platform": name.value,
                    "state": state.state.value,
                    "diagnostic_code": state.diagnostic_code,
                }
                if not state.authenticated:
                    instruction = await adapter.begin_login()
                    row["action"] = instruction.action.value
                    row["message"] = instruction.message
                    row["checkpoint"] = instruction.checkpoint
                    needs_action = needs_action or instruction.requires_user_action
                rows.append(row)
        return rows, needs_action

    try:
        rows, needs_action = asyncio.run(operation())
    except Exception:
        _fail(
            "Browser session or platform login check failed; run smfa doctor.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    _emit({"sessions": rows}, json_output=json_output)
    if needs_action:
        raise typer.Exit(code=int(ExitCode.USER_ACTION_REQUIRED))


@app.command()
def collections(
    platform: Annotated[str, typer.Argument(help="Platform name or all.")] = "all",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """List authorized favorite collections without writing archive state."""
    settings = _settings(config, json_output=json_output)
    selected = _platforms(platform, settings, json_output=json_output)

    async def operation() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        async with open_adapters(settings, selected) as adapters:
            for name in selected:
                for entry in await _collection_list(adapters[name]):
                    rows.append(
                        {
                            "platform": name.value,
                            "id": entry.platform_collection_id,
                            "canonical_id": entry.canonical_id,
                            "name": entry.name,
                        }
                    )
        return rows

    try:
        rows = asyncio.run(operation())
    except Exception:
        _fail(
            "Collection discovery failed; check login and sanitized diagnostics.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    _emit({"collections": rows}, json_output=json_output)


@app.command()
def sync(
    platform: Annotated[str, typer.Argument(help="Platform name or all.")] = "all",
    collection: Annotated[
        list[str] | None,
        typer.Option("--collection", help="Collection ID, canonical ID, or exact name."),
    ] = None,
    metadata_only: Annotated[
        bool,
        typer.Option("--metadata-only", help="Render skeletons and leave heavy jobs queued."),
    ] = False,
    foreground: Annotated[
        bool,
        typer.Option("--foreground", help="Drain the durable heavy queue in this process."),
    ] = False,
    full: Annotated[
        bool,
        typer.Option("--full", help="Disable early-stop and permit complete reconciliation."),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Bound observed items for validation."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Inspect selected collections without archive writes."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Synchronize favorites and optionally drain durable heavy work."""
    if metadata_only and foreground:
        _fail(
            "--metadata-only and --foreground cannot be combined.",
            code=ExitCode.INVALID_USAGE,
            json_output=json_output,
        )
    settings = _settings(config, json_output=json_output)
    selected_platforms = _platforms(platform, settings, json_output=json_output)

    async def operation() -> dict[str, object]:
        async with open_adapters(settings, selected_platforms) as adapters:
            selected_collections: list[tuple[BaseAdapter, Collection]] = []
            for name in selected_platforms:
                adapter = adapters[name]
                available = await _collection_list(adapter)
                chosen = _select_collections(available, collection)
                selected_collections.extend((adapter, entry) for entry in chosen)
            if collection and not selected_collections:
                raise LookupError("no collection matched the filter")
            if dry_run:
                return {
                    "dry_run": True,
                    "collections": [
                        {
                            "platform": entry.platform.value,
                            "id": entry.platform_collection_id,
                            "name": entry.name,
                        }
                        for _, entry in selected_collections
                    ],
                }

            database = Database(settings.state_db_path)
            database.migrate()
            orchestrator = SyncOrchestrator(database, MarkdownRenderer(settings.vault_path))
            results: list[SyncResult] = []
            for adapter, entry in selected_collections:
                results.append(
                    await orchestrator.enumerate_collection(
                        adapter,
                        entry,
                        options=SyncOptions(
                            early_stop_threshold=settings.early_stop_threshold,
                            force_full_sync=full,
                            item_limit=limit,
                        ),
                    )
                )
            snapshot = build_status(database)
            pending = sum(
                snapshot.jobs.get(status, 0)
                for status in ("pending", "retryable", "running")
            )
            heavy: dict[str, int | bool] = {
                "drain_requested": False,
                "drained": 0,
                "failures": 0,
                "pending": pending,
            }
            if foreground and not metadata_only:
                resources = CommandResources(
                    settings=settings,
                    database=database,
                    queue=JobQueue(database),
                    orchestrator=orchestrator,
                    adapters=adapters,
                )
                heavy = await _drain_foreground(resources)
            return {
                "dry_run": False,
                "run_ids": [result.run_id for result in results],
                "enumeration": {
                    "references_observed": sum(
                        result.references_observed for result in results
                    ),
                    "items_fetched": sum(result.items_fetched for result in results),
                    "unchanged_items": sum(result.unchanged_items for result in results),
                    "skeletons_rendered": sum(
                        result.skeletons_rendered for result in results
                    ),
                    "jobs_enqueued": sum(result.jobs_enqueued for result in results),
                    "memberships_removed": sum(
                        result.memberships_removed for result in results
                    ),
                    "limited": any(result.limited for result in results),
                    "early_stopped": any(result.early_stopped for result in results),
                    "complete": bool(results)
                    and all(result.enumeration_complete for result in results),
                },
                "heavy": heavy,
            }

    try:
        payload = asyncio.run(operation())
    except (KeyboardInterrupt, asyncio.CancelledError):
        _fail(
            "Synchronization cancelled at a safe checkpoint.",
            code=ExitCode.INTERRUPTED,
            json_output=json_output,
        )
    except LookupError:
        _fail(
            "No collection matched the requested filter.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    except Exception:
        _fail(
            "Synchronization failed; inspect smfa status and smfa report.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    if json_output:
        _emit(payload, json_output=True)
    elif payload.get("dry_run"):
        dry_collections = payload.get("collections")
        selected_count = len(dry_collections) if isinstance(dry_collections, list) else 0
        typer.echo(f"dry-run: {selected_count} collections selected")
    else:
        enumeration = payload["enumeration"]
        heavy = payload["heavy"]
        assert isinstance(enumeration, dict) and isinstance(heavy, dict)
        typer.echo(
            "enumeration: "
            f"observed={enumeration['references_observed']} "
            f"skeletons={enumeration['skeletons_rendered']} "
            f"complete={enumeration['complete']}"
        )
        typer.echo(
            "heavy: "
            f"drained={heavy['drained']} pending={heavy['pending']} "
            f"failures={heavy['failures']}"
        )
    if not payload.get("dry_run"):
        heavy = payload.get("heavy")
        if isinstance(heavy, dict) and int(heavy.get("failures", 0)) > 0:
            raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR))


@app.command()
def status(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Show local synchronization and queue status."""
    settings = _settings(config, json_output=json_output)
    snapshot = build_status(Database(settings.state_db_path))
    _emit(snapshot.model_dump(mode="json"), json_output=json_output)


@app.command()
def retry(
    target: Annotated[str, typer.Argument(help="Job ID or 'failed'.")] = "failed",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Requeue one failed job or all retryable/blocked/failed jobs."""
    settings = _settings(config, json_output=json_output)
    database = Database(settings.state_db_path)
    database.migrate()
    queue = JobQueue(database)
    if target == "failed":
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status IN ('retryable', 'blocked', 'failed') ORDER BY created_at, id
                """
            ).fetchall()
        job_ids = [str(row["id"]) for row in rows]
    else:
        job_ids = [target]
    retried = 0
    try:
        for job_id in job_ids:
            queue.retry(job_id)
            retried += 1
    except (KeyError, InvalidJobTransition):
        _fail(
            "The requested job does not exist or is not retryable.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    if not job_ids and target != "failed":
        _fail(
            "The requested job does not exist.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    _emit({"retried": retried}, json_output=json_output)


def _ready_barrier() -> DerivativeBarrier:
    return DerivativeBarrier.model_validate(
        {name: BarrierState.SUCCEEDED for name in DerivativeBarrier.model_fields}
    )


@app.command()
def cleanup(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Delete only files behind verified cleanup jobs."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Explicitly preview without deleting files."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Preview or remove verified item-owned temporary files."""
    if apply and dry_run:
        _fail(
            "--apply and --dry-run cannot be combined.",
            code=ExitCode.INVALID_USAGE,
            json_output=json_output,
        )
    settings = _settings(config, json_output=json_output)
    database = Database(settings.state_db_path)
    if not database.path.is_file() or database.current_schema_version() == 0:
        _emit({"dry_run": not apply, "eligible": 0, "cleaned": 0}, json_output=json_output)
        return
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT items.canonical_id, assets.local_path
            FROM jobs
            JOIN items ON items.id = jobs.item_id
            JOIN assets ON assets.item_id = items.id
            WHERE jobs.stage = 'cleanup'
              AND jobs.status IN ('pending', 'retryable', 'succeeded')
              AND assets.cleanup_status = 'retained'
              AND assets.local_path IS NOT NULL
            ORDER BY items.canonical_id, assets.local_path
            """
        ).fetchall()
    manager = CleanupManager(database, AssetPathManager(settings.cache_path))
    eligible = 0
    cleaned = 0
    for row in rows:
        try:
            result = manager.cleanup(
                str(row["canonical_id"]),
                str(row["local_path"]),
                barrier=_ready_barrier(),
                dry_run=not apply,
            )
        except Exception:
            _fail(
                "Cleanup verification failed; no unverified file was removed.",
                code=ExitCode.OPERATIONAL_ERROR,
                json_output=json_output,
            )
        eligible += int(result.status == "eligible")
        cleaned += int(result.status == "cleaned")
    _emit(
        {"dry_run": not apply, "eligible": eligible, "cleaned": cleaned},
        json_output=json_output,
    )


@app.command()
def report(
    run_id: Annotated[str | None, typer.Argument(help="Run ID; defaults to latest.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Generate a sanitized per-run report."""
    settings = _settings(config, json_output=json_output)
    try:
        result = build_run_report(Database(settings.state_db_path), run_id)
    except KeyError:
        _fail(
            "No matching synchronization run exists.",
            code=ExitCode.OPERATIONAL_ERROR,
            json_output=json_output,
        )
    _emit(result.model_dump(mode="json"), json_output=json_output)


if __name__ == "__main__":
    app()
