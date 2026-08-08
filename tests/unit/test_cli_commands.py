from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from social_media_favorites_archiver.cli import app
from social_media_favorites_archiver.models import Platform
from social_media_favorites_archiver.queue import JobQueue, ProcessingStage
from social_media_favorites_archiver.storage.database import Database

runner = CliRunner()
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(
        "\n".join(
            (
                f"vault_path: {tmp_path / 'vault'}",
                f"state_db_path: {tmp_path / 'archive.db'}",
                f"cache_path: {tmp_path / 'cache'}",
                f"browser_profile_path: {tmp_path / 'browser'}",
            )
        ),
        encoding="utf-8",
    )
    return path


def test_help_exposes_complete_command_surface_and_sync_modes() -> None:
    result = runner.invoke(app, ["--help"])
    sync_help = runner.invoke(app, ["sync", "--help"])

    assert result.exit_code == 0, result.output
    for command in (
        "doctor",
        "login",
        "collections",
        "sync",
        "status",
        "retry",
        "cleanup",
        "report",
    ):
        assert command in result.output
    for option in (
        "--collection",
        "--metadata-only",
        "--foreground",
        "--full",
        "--limit",
        "--dry-run",
        "--json",
    ):
        assert option in sync_help.output


def test_invalid_configuration_uses_stable_usage_exit_code(tmp_path: Path) -> None:
    config = tmp_path / "invalid.yml"
    config.write_text("concurrency: 0\n", encoding="utf-8")

    result = runner.invoke(app, ["status", "--config", str(config), "--json"])

    assert result.exit_code == 2
    assert "invalid or unreadable" in result.output.lower()


def test_status_retry_and_report_are_json_safe(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database = Database(tmp_path / "archive.db")
    database.migrate()
    queue = JobQueue(database)
    job = queue.enqueue(
        platform=Platform.BILIBILI,
        stage=ProcessingStage.ASSETS,
        idempotency_key="fixture-cli-job",
        now=NOW,
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = 'failed', last_diagnostic_code = 'fixture.failed'
            WHERE id = ?
            """,
            (job.id,),
        )
        connection.execute(
            """
            INSERT INTO runs(
                id, platform, started_at, finished_at, status,
                enumeration_complete, stats_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-fixture",
                "bilibili",
                NOW.isoformat(),
                NOW.isoformat(),
                "failed",
                0,
                json.dumps(
                    {
                        "references_observed": 1,
                        "checkpoint_cursor": "private-cursor-value",
                        "raw_error": "Authorization: Bearer private-provider-value",
                    }
                ),
            ),
        )

    status_result = runner.invoke(app, ["status", "--config", str(config), "--json"])
    retry_result = runner.invoke(
        app,
        ["retry", job.id, "--config", str(config), "--json"],
    )
    report_result = runner.invoke(
        app,
        ["report", "run-fixture", "--config", str(config), "--json"],
    )

    assert status_result.exit_code == 0, status_result.output
    assert json.loads(status_result.output)["jobs"]["failed"] == 1
    assert retry_result.exit_code == 0, retry_result.output
    assert json.loads(retry_result.output)["retried"] == 1
    assert JobQueue(database).get(job.id).status.value == "pending"
    assert report_result.exit_code == 0, report_result.output
    report = json.loads(report_result.output)
    assert report["run_id"] == "run-fixture"
    assert report["failures"] == [{"diagnostic_code": "fixture.failed", "count": 1}]
    assert "private-cursor-value" not in report_result.output
    assert "private-provider-value" not in report_result.output


def test_unknown_job_and_missing_report_use_operational_exit_code(tmp_path: Path) -> None:
    config = _config(tmp_path)

    retry_result = runner.invoke(
        app,
        ["retry", "missing-job", "--config", str(config), "--json"],
    )
    report_result = runner.invoke(
        app,
        ["report", "missing-run", "--config", str(config), "--json"],
    )

    assert retry_result.exit_code == 1
    assert report_result.exit_code == 1


def test_cleanup_preview_is_read_only_when_database_is_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = runner.invoke(
        app,
        ["cleanup", "--dry-run", "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"cleaned": 0, "dry_run": True, "eligible": 0}
    assert not (tmp_path / "archive.db").exists()
