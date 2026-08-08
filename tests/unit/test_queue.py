from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from filelock import Timeout

from social_media_favorites_archiver.models import Platform
from social_media_favorites_archiver.queue import (
    InvalidJobTransition,
    ItemFileLocks,
    JobQueue,
    JobStatus,
    ProcessingStage,
    assert_transition,
    retry_delay_seconds,
)
from social_media_favorites_archiver.storage.database import Database

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("current", "target"),
    (
        (JobStatus.PENDING, JobStatus.RUNNING),
        (JobStatus.RUNNING, JobStatus.SUCCEEDED),
        (JobStatus.RUNNING, JobStatus.RETRYABLE),
        (JobStatus.RUNNING, JobStatus.NEEDS_AUTH),
        (JobStatus.RUNNING, JobStatus.BLOCKED),
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.RETRYABLE, JobStatus.RUNNING),
        (JobStatus.NEEDS_AUTH, JobStatus.PENDING),
        (JobStatus.BLOCKED, JobStatus.PENDING),
    ),
)
def test_legal_job_transitions(current: JobStatus, target: JobStatus) -> None:
    assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    (
        (JobStatus.PENDING, JobStatus.SUCCEEDED),
        (JobStatus.SUCCEEDED, JobStatus.RUNNING),
        (JobStatus.FAILED, JobStatus.PENDING),
        (JobStatus.RETRYABLE, JobStatus.SUCCEEDED),
    ),
)
def test_illegal_job_transitions_raise(current: JobStatus, target: JobStatus) -> None:
    with pytest.raises(InvalidJobTransition):
        assert_transition(current, target)


def test_retry_delay_is_exponential_capped_and_deterministic_with_injected_jitter() -> None:
    assert retry_delay_seconds(1, base_seconds=2, cap_seconds=10, jitter_ratio=0.5, jitter=0.5) == 2
    assert retry_delay_seconds(2, base_seconds=2, cap_seconds=10, jitter_ratio=0.5, jitter=1.0) == 6
    assert retry_delay_seconds(10, base_seconds=2, cap_seconds=10, jitter_ratio=0.5, jitter=1.0) == 15


def test_enqueue_is_idempotent_and_retry_waits_until_due(tmp_path: Path) -> None:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    queue = JobQueue(database)

    first = queue.enqueue(
        platform=Platform.BILIBILI,
        stage=ProcessingStage.ASSETS,
        idempotency_key="bilibili:BV1example:assets:v1",
        now=NOW,
    )
    duplicate = queue.enqueue(
        platform=Platform.BILIBILI,
        stage=ProcessingStage.ASSETS,
        idempotency_key="bilibili:BV1example:assets:v1",
        now=NOW,
    )
    assert duplicate.id == first.id

    leased = queue.lease_next("worker-a", now=NOW, lease_seconds=30)
    assert leased is not None
    retry_at = queue.release_for_retry(
        leased.id,
        "worker-a",
        diagnostic_code="network.timeout",
        now=NOW,
        base_seconds=2,
        cap_seconds=10,
        jitter_ratio=0,
    )
    assert retry_at == NOW + timedelta(seconds=2)
    assert queue.lease_next("worker-b", now=NOW + timedelta(seconds=1)) is None
    assert queue.lease_next("worker-b", now=retry_at) is not None


def test_file_lock_serializes_item_writes(tmp_path: Path) -> None:
    locks = ItemFileLocks(tmp_path / "locks")

    with locks.acquire("bilibili:BV1example", timeout=0):
        with pytest.raises(Timeout), locks.acquire("bilibili:BV1example", timeout=0):
            pass

