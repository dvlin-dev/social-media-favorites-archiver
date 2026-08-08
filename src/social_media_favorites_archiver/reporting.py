"""Sanitized queue status and per-run reports."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from social_media_favorites_archiver.queue import JobStatus
from social_media_favorites_archiver.storage.database import Database

_SAFE_STATS = {
    "references_observed",
    "items_fetched",
    "unchanged_items",
    "refavorited",
    "skeletons_rendered",
    "jobs_enqueued",
    "memberships_removed",
    "early_stopped",
    "limited",
}
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureCount(_FrozenModel):
    diagnostic_code: str
    count: int = Field(ge=1)


class RunReport(_FrozenModel):
    run_id: str
    platform: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None = Field(default=None, ge=0)
    enumeration_complete: bool
    run_counts: dict[str, int | bool]
    platforms: dict[str, int]
    content_types: dict[str, int]
    stages: dict[str, dict[str, int]]
    failures: tuple[FailureCount, ...]
    needs_auth_actions: tuple[str, ...]
    cleanup: dict[str, int]
    next_safe_commands: tuple[str, ...]


class StatusSnapshot(_FrozenModel):
    database_present: bool
    schema_version: int = Field(ge=0)
    runs: dict[str, int]
    jobs: dict[str, int]
    platforms: dict[str, int]
    content_types: dict[str, int]
    latest_run: dict[str, object] | None = None


def _empty_status() -> StatusSnapshot:
    return StatusSnapshot(
        database_present=False,
        schema_version=0,
        runs={},
        jobs={status.value: 0 for status in JobStatus},
        platforms={},
        content_types={},
    )


def _counts(rows: list[Any], key: str, count: str = "count") -> dict[str, int]:
    return {str(row[key]): int(row[count]) for row in rows}


def build_status(database: Database) -> StatusSnapshot:
    """Read local aggregate state without creating a missing database."""
    if not database.path.is_file():
        return _empty_status()
    schema_version = database.current_schema_version()
    if schema_version == 0:
        return StatusSnapshot(
            database_present=True,
            schema_version=0,
            runs={},
            jobs={status.value: 0 for status in JobStatus},
            platforms={},
            content_types={},
        )
    with database.connect() as connection:
        runs = connection.execute(
            "SELECT status, COUNT(*) AS count FROM runs GROUP BY status ORDER BY status"
        ).fetchall()
        jobs = connection.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
        ).fetchall()
        platforms = connection.execute(
            "SELECT platform, COUNT(*) AS count FROM items GROUP BY platform ORDER BY platform"
        ).fetchall()
        content_types = connection.execute(
            """
            SELECT content_type, COUNT(*) AS count
            FROM items GROUP BY content_type ORDER BY content_type
            """
        ).fetchall()
        latest = connection.execute(
            """
            SELECT id, platform, status, started_at, finished_at, enumeration_complete
            FROM runs ORDER BY started_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
    job_counts = {status.value: 0 for status in JobStatus}
    job_counts.update(_counts(list(jobs), "status"))
    latest_run: dict[str, object] | None = (
        None
        if latest is None
        else {
            "run_id": str(latest["id"]),
            "platform": str(latest["platform"]),
            "status": str(latest["status"]),
            "started_at": str(latest["started_at"]),
            "finished_at": None
            if latest["finished_at"] is None
            else str(latest["finished_at"]),
            "enumeration_complete": bool(latest["enumeration_complete"]),
        }
    )
    return StatusSnapshot(
        database_present=True,
        schema_version=schema_version,
        runs=_counts(list(runs), "status"),
        jobs=job_counts,
        platforms=_counts(list(platforms), "platform"),
        content_types=_counts(list(content_types), "content_type"),
        latest_run=latest_run,
    )


def _safe_stats(raw: object) -> dict[str, int | bool]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int | bool] = {}
    for key in sorted(_SAFE_STATS):
        value = raw.get(key)
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int) and value >= 0:
            result[key] = value
    return result


def _duration(started: datetime, finished: datetime | None) -> float | None:
    if finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


def build_run_report(database: Database, run_id: str | None = None) -> RunReport:
    """Build one report from whitelisted counts and sanitized diagnostic codes."""
    if not database.path.is_file() or database.current_schema_version() == 0:
        raise KeyError(run_id or "latest")
    with database.connect() as connection:
        if run_id is None:
            run = connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
        else:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id or "latest")
        platform = str(run["platform"])
        content_type_rows = connection.execute(
            """
            SELECT content_type, COUNT(*) AS count
            FROM items WHERE platform = ? GROUP BY content_type ORDER BY content_type
            """,
            (platform,),
        ).fetchall()
        stage_rows = connection.execute(
            """
            SELECT stage, status, COUNT(*) AS count
            FROM jobs WHERE platform = ? GROUP BY stage, status ORDER BY stage, status
            """,
            (platform,),
        ).fetchall()
        failure_rows = connection.execute(
            """
            SELECT last_diagnostic_code, COUNT(*) AS count
            FROM jobs
            WHERE platform = ?
              AND last_diagnostic_code IS NOT NULL
            GROUP BY last_diagnostic_code ORDER BY last_diagnostic_code
            """,
            (platform,),
        ).fetchall()
        cleanup_rows = connection.execute(
            """
            SELECT assets.cleanup_status, COUNT(*) AS count
            FROM assets JOIN items ON items.id = assets.item_id
            WHERE items.platform = ?
            GROUP BY assets.cleanup_status ORDER BY assets.cleanup_status
            """,
            (platform,),
        ).fetchall()
        needs_auth = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE platform = ? AND status = 'needs_auth'",
            (platform,),
        ).fetchone()[0]
        failed = connection.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE platform = ? AND status IN ('retryable', 'blocked', 'failed')
            """,
            (platform,),
        ).fetchone()[0]
        pending = connection.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE platform = ? AND status IN ('pending', 'retryable', 'running')
            """,
            (platform,),
        ).fetchone()[0]
    try:
        parsed_stats = json.loads(str(run["stats_json"]))
    except (TypeError, ValueError):
        parsed_stats = {}
    started = datetime.fromisoformat(str(run["started_at"]))
    finished = (
        None
        if run["finished_at"] is None
        else datetime.fromisoformat(str(run["finished_at"]))
    )
    references = _safe_stats(parsed_stats).get("references_observed", 0)
    platform_count = int(references) if isinstance(references, int) else 0
    stages: dict[str, dict[str, int]] = {}
    for row in stage_rows:
        stages.setdefault(str(row["stage"]), {})[str(row["status"])] = int(row["count"])
    failures = tuple(
        FailureCount(
            diagnostic_code=(
                str(row["last_diagnostic_code"])
                if _SAFE_CODE.fullmatch(str(row["last_diagnostic_code"]))
                else "report.redacted"
            ),
            count=int(row["count"]),
        )
        for row in failure_rows
    )
    actions = (f"smfa login {platform}",) if int(needs_auth) else ()
    next_commands: list[str] = []
    if int(needs_auth):
        next_commands.append(f"smfa login {platform}")
    if int(failed):
        next_commands.append("smfa retry failed")
    if int(pending):
        next_commands.append(f"smfa sync {platform} --foreground")
    if not next_commands:
        next_commands.append(f"smfa report {run['id']} --json")
    return RunReport(
        run_id=str(run["id"]),
        platform=platform,
        status=str(run["status"]),
        started_at=started,
        finished_at=finished,
        duration_seconds=_duration(started, finished),
        enumeration_complete=bool(run["enumeration_complete"]),
        run_counts=_safe_stats(parsed_stats),
        platforms={platform: platform_count},
        content_types=_counts(list(content_type_rows), "content_type"),
        stages=stages,
        failures=failures,
        needs_auth_actions=actions,
        cleanup=_counts(list(cleanup_rows), "cleanup_status"),
        next_safe_commands=tuple(next_commands),
    )
