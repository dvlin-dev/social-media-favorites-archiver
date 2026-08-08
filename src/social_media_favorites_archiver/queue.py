"""Durable SQLite jobs, worker leases, and per-item file locks."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from filelock import FileLock
from pydantic import BaseModel, ConfigDict

from social_media_favorites_archiver.models import Platform
from social_media_favorites_archiver.storage.database import Database


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    NEEDS_AUTH = "needs_auth"
    BLOCKED = "blocked"
    FAILED = "failed"


class ProcessingStage(StrEnum):
    ENUMERATION = "enumeration"
    METADATA = "metadata"
    SKELETON = "skeleton"
    ASSETS = "assets"
    EXTRACTION = "extraction"
    FUSION = "fusion"
    ENRICHMENT = "enrichment"
    RENDER = "render"
    VERIFY = "verify"
    CLEANUP = "cleanup"


class InvalidJobTransition(ValueError):
    """Raised when a job attempts an illegal state change."""


_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset(
        {JobStatus.RUNNING, JobStatus.NEEDS_AUTH, JobStatus.BLOCKED, JobStatus.FAILED}
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.RETRYABLE,
            JobStatus.NEEDS_AUTH,
            JobStatus.BLOCKED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.RETRYABLE: frozenset(
        {JobStatus.RUNNING, JobStatus.NEEDS_AUTH, JobStatus.BLOCKED, JobStatus.FAILED}
    ),
    JobStatus.NEEDS_AUTH: frozenset({JobStatus.PENDING, JobStatus.FAILED}),
    JobStatus.BLOCKED: frozenset({JobStatus.PENDING, JobStatus.FAILED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


def assert_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in _TRANSITIONS[current]:
        raise InvalidJobTransition(f"illegal job transition: {current.value} -> {target.value}")


def retry_delay_seconds(
    attempt: int,
    *,
    base_seconds: float = 2,
    cap_seconds: float = 300,
    jitter_ratio: float = 0.25,
    jitter: float = 0.5,
) -> float:
    """Return capped exponential delay with deterministic injectable jitter."""
    if attempt < 1:
        msg = "attempt must be at least one"
        raise ValueError(msg)
    if base_seconds <= 0 or cap_seconds <= 0:
        msg = "retry delays must be positive"
        raise ValueError(msg)
    if not 0 <= jitter_ratio <= 1 or not 0 <= jitter <= 1:
        msg = "jitter inputs must be between zero and one"
        raise ValueError(msg)
    exponential: float = min(cap_seconds, base_seconds * (2.0 ** (attempt - 1)))
    multiplier = 1 + jitter_ratio * ((2 * jitter) - 1)
    return exponential * multiplier


class JobRecord(BaseModel):
    """Persisted job state returned to workers."""

    model_config = ConfigDict(frozen=True)

    id: str
    item_id: int | None
    platform: Platform
    stage: ProcessingStage
    status: JobStatus
    idempotency_key: str
    attempts: int
    next_attempt_at: datetime | None
    lease_owner: str | None
    lease_until: datetime | None
    last_diagnostic_code: str | None
    created_at: datetime
    updated_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "queue timestamps must include timezone information"
        raise ValueError(msg)
    return value.astimezone(UTC).isoformat()


def _job_id(idempotency_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"smfa-job:{idempotency_key}"))


_DIAGNOSTIC_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _validate_diagnostic_code(code: str) -> str:
    if _DIAGNOSTIC_CODE.fullmatch(code) is None:
        msg = "diagnostic code must be a short lowercase identifier, not a raw error"
        raise ValueError(msg)
    return code


class ItemFileLocks:
    """Filesystem lock boundary for Markdown rendering and cleanup."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def acquire(self, canonical_id: str, *, timeout: float = 30) -> Iterator[None]:
        digest = hashlib.sha256(canonical_id.encode("utf-8")).hexdigest()
        lock = FileLock(self.root / f"{digest}.lock")
        with lock.acquire(timeout=timeout):
            yield


class JobQueue:
    """Persistent, idempotent jobs with atomic SQLite leases."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> JobRecord:
        mapping = {key: row[key] for key in row.keys()}
        return JobRecord.model_validate(mapping)

    def enqueue(
        self,
        *,
        platform: Platform,
        stage: ProcessingStage,
        idempotency_key: str,
        item_id: int | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        current_time = now or _now()
        timestamp = _timestamp(current_time)
        identifier = _job_id(idempotency_key)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, item_id, platform, stage, status, idempotency_key,
                    attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    identifier,
                    item_id,
                    platform.value,
                    stage.value,
                    idempotency_key,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            msg = "job enqueue did not return a record"
            raise RuntimeError(msg)
        return self._from_row(row)

    def get(self, job_id: str) -> JobRecord:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._from_row(row)

    def lease_next(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: float = 60,
        platform: Platform | None = None,
    ) -> JobRecord | None:
        if not worker_id:
            msg = "worker_id must be non-empty"
            raise ValueError(msg)
        if lease_seconds <= 0:
            msg = "lease_seconds must be positive"
            raise ValueError(msg)
        current_time = now or _now()
        current = _timestamp(current_time)
        lease_until = _timestamp(current_time + timedelta(seconds=lease_seconds))
        platform_clause = "" if platform is None else "AND jobs.platform = ?"
        parameters: list[object] = [current, current, current]
        if platform is not None:
            parameters.append(platform.value)
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                f"""
                SELECT jobs.*
                FROM jobs
                WHERE (
                    (
                        jobs.status IN ('pending', 'retryable')
                        AND (jobs.next_attempt_at IS NULL OR jobs.next_attempt_at <= ?)
                    )
                    OR (
                        jobs.status = 'running'
                        AND jobs.lease_until IS NOT NULL
                        AND jobs.lease_until <= ?
                    )
                )
                AND (
                    jobs.item_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1
                        FROM jobs AS active
                        WHERE active.item_id = jobs.item_id
                          AND active.id != jobs.id
                          AND active.status = 'running'
                          AND active.lease_until > ?
                    )
                )
                {platform_clause}
                ORDER BY
                    CASE WHEN jobs.status = 'running' THEN 0 ELSE 1 END,
                    jobs.created_at,
                    jobs.id
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', lease_owner = ?, lease_until = ?,
                    attempts = attempts + 1, next_attempt_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_until, current, row["id"]),
            )
            leased = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
        if leased is None:
            msg = "leased job disappeared"
            raise RuntimeError(msg)
        return self._from_row(leased)

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: float = 60,
    ) -> bool:
        current_time = now or _now()
        current = _timestamp(current_time)
        extended = _timestamp(current_time + timedelta(seconds=lease_seconds))
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ? AND lease_until > ?
                """,
                (extended, current, job_id, worker_id, current),
            )
            return cursor.rowcount == 1

    def _owned_transition(
        self,
        job_id: str,
        worker_id: str,
        target: JobStatus,
        *,
        diagnostic_code: str | None = None,
        next_attempt_at: datetime | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        if diagnostic_code is not None:
            diagnostic_code = _validate_diagnostic_code(diagnostic_code)
        current_time = now or _now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            current_status = JobStatus(row["status"])
            assert_transition(current_status, target)
            if current_status == JobStatus.RUNNING and row["lease_owner"] != worker_id:
                msg = "worker does not own the active lease"
                raise PermissionError(msg)
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_until = NULL, last_diagnostic_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target.value,
                    _timestamp(next_attempt_at) if next_attempt_at else None,
                    diagnostic_code,
                    _timestamp(current_time),
                    job_id,
                ),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if updated is None:
            msg = "transitioned job disappeared"
            raise RuntimeError(msg)
        return self._from_row(updated)

    def complete(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> JobRecord:
        return self._owned_transition(job_id, worker_id, JobStatus.SUCCEEDED, now=now)

    def release_for_retry(
        self,
        job_id: str,
        worker_id: str,
        *,
        diagnostic_code: str,
        now: datetime | None = None,
        base_seconds: float = 2,
        cap_seconds: float = 300,
        jitter_ratio: float = 0.25,
        jitter: float = 0.5,
    ) -> datetime:
        current_time = now or _now()
        job = self.get(job_id)
        delay = retry_delay_seconds(
            job.attempts,
            base_seconds=base_seconds,
            cap_seconds=cap_seconds,
            jitter_ratio=jitter_ratio,
            jitter=jitter,
        )
        retry_at = current_time + timedelta(seconds=delay)
        self._owned_transition(
            job_id,
            worker_id,
            JobStatus.RETRYABLE,
            diagnostic_code=diagnostic_code,
            next_attempt_at=retry_at,
            now=current_time,
        )
        return retry_at

    def mark_needs_auth(
        self,
        job_id: str,
        worker_id: str,
        *,
        diagnostic_code: str,
        now: datetime | None = None,
    ) -> JobRecord:
        return self._owned_transition(
            job_id,
            worker_id,
            JobStatus.NEEDS_AUTH,
            diagnostic_code=diagnostic_code,
            now=now,
        )

    def retry(self, job_id: str, *, now: datetime | None = None) -> JobRecord:
        """Explicitly requeue a failed, blocked, auth, or retryable job."""
        current_time = now or _now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            current_status = JobStatus(row["status"])
            if current_status not in {
                JobStatus.RETRYABLE,
                JobStatus.NEEDS_AUTH,
                JobStatus.BLOCKED,
                JobStatus.FAILED,
            }:
                raise InvalidJobTransition(
                    f"job is not explicitly retryable: {current_status.value}"
                )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', next_attempt_at = NULL,
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_timestamp(current_time), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("retried job disappeared")
        return self._from_row(updated)

    def release_cancelled(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> JobRecord:
        """Release an owned lease immediately when foreground work is cancelled."""
        current_time = now or _now()
        return self._owned_transition(
            job_id,
            worker_id,
            JobStatus.RETRYABLE,
            diagnostic_code="worker.cancelled",
            next_attempt_at=current_time,
            now=current_time,
        )
