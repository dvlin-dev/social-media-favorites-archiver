from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from social_media_favorites_archiver.models import ContentType, NormalizedItem, Platform
from social_media_favorites_archiver.queue import JobQueue, JobStatus, ProcessingStage
from social_media_favorites_archiver.storage.database import Database

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SHA = f"sha256:{'a' * 64}"


def _database_and_item(tmp_path: Path) -> tuple[Database, int]:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    item_id = database.upsert_item(
        NormalizedItem(
            canonical_id="bilibili:BV1example",
            platform=Platform.BILIBILI,
            content_type=ContentType.VIDEO,
            source_url="https://www.bilibili.com/video/BV1example",
            title="Example video",
            author="Example author",
            first_seen_at=NOW,
            last_seen_at=NOW,
            metadata_fingerprint=SHA,
            adapter_version="fixture-v1",
        )
    )
    return database, item_id


def test_only_one_worker_can_lease_a_job_and_item(tmp_path: Path) -> None:
    database, item_id = _database_and_item(tmp_path)
    queue = JobQueue(database)
    queue.enqueue(
        item_id=item_id,
        platform=Platform.BILIBILI,
        stage=ProcessingStage.ASSETS,
        idempotency_key="item:assets:v1",
        now=NOW,
    )
    queue.enqueue(
        item_id=item_id,
        platform=Platform.BILIBILI,
        stage=ProcessingStage.EXTRACTION,
        idempotency_key="item:extraction:v1",
        now=NOW,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(
            executor.map(
                lambda worker: queue.lease_next(worker, now=NOW, lease_seconds=30),
                (f"worker-{number}" for number in range(8)),
            )
        )

    acquired = [lease for lease in leases if lease is not None]
    assert len(acquired) == 1
    assert queue.lease_next("worker-late", now=NOW, lease_seconds=30) is None

    queue.complete(acquired[0].id, acquired[0].lease_owner or "")
    assert queue.lease_next("worker-next", now=NOW, lease_seconds=30) is not None


def test_expired_lease_is_recovered_and_heartbeat_extends_active_lease(tmp_path: Path) -> None:
    database, item_id = _database_and_item(tmp_path)
    queue = JobQueue(database)
    enqueued = queue.enqueue(
        item_id=item_id,
        platform=Platform.BILIBILI,
        stage=ProcessingStage.ASSETS,
        idempotency_key="item:assets:v1",
        now=NOW,
    )
    first = queue.lease_next("worker-a", now=NOW, lease_seconds=10)
    assert first is not None
    assert queue.heartbeat(
        enqueued.id,
        "worker-a",
        now=NOW + timedelta(seconds=5),
        lease_seconds=20,
    )
    assert queue.lease_next("worker-b", now=NOW + timedelta(seconds=11)) is None

    recovered = queue.lease_next("worker-b", now=NOW + timedelta(seconds=26), lease_seconds=10)
    assert recovered is not None
    assert recovered.id == enqueued.id
    assert recovered.lease_owner == "worker-b"
    assert recovered.attempts == 2


def test_needs_auth_on_one_platform_does_not_block_another(tmp_path: Path) -> None:
    database = Database(tmp_path / "archive.db")
    database.migrate()
    queue = JobQueue(database)
    queue.enqueue(
        platform=Platform.BILIBILI,
        stage=ProcessingStage.ENUMERATION,
        idempotency_key="bilibili:enumeration:v1",
        now=NOW,
    )
    queue.enqueue(
        platform=Platform.DOUYIN,
        stage=ProcessingStage.ENUMERATION,
        idempotency_key="douyin:enumeration:v1",
        now=NOW,
    )

    bilibili = queue.lease_next(
        "worker-bili",
        platform=Platform.BILIBILI,
        now=NOW,
        lease_seconds=30,
    )
    assert bilibili is not None
    queue.mark_needs_auth(bilibili.id, "worker-bili", diagnostic_code="session.expired")

    douyin = queue.lease_next(
        "worker-douyin",
        platform=Platform.DOUYIN,
        now=NOW,
        lease_seconds=30,
    )
    assert douyin is not None
    assert douyin.platform == Platform.DOUYIN
    assert queue.get(bilibili.id).status == JobStatus.NEEDS_AUTH
