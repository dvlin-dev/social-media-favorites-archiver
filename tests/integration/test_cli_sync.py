from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from typer.testing import CliRunner

import social_media_favorites_archiver.cli as cli
from social_media_favorites_archiver.adapters.base import (
    LoginAction,
    LoginInstruction,
    SessionState,
    SessionStatus,
)
from social_media_favorites_archiver.config import AppSettings
from social_media_favorites_archiver.models import Platform
from social_media_favorites_archiver.queue import JobRecord
from social_media_favorites_archiver.storage.database import Database
from tests.integration.orchestrator_fakes import FixtureAdapter

runner = CliRunner()


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(
        "\n".join(
            (
                f"vault_path: {tmp_path / 'vault'}",
                f"state_db_path: {tmp_path / 'archive.db'}",
                f"cache_path: {tmp_path / 'cache'}",
                f"browser_profile_path: {tmp_path / 'browser'}",
                "enabled_platforms: [bilibili]",
            )
        ),
        encoding="utf-8",
    )
    return path


def _install_adapter(monkeypatch, adapter: FixtureAdapter) -> None:
    @asynccontextmanager
    async def fake_open(
        settings: AppSettings,
        platforms: tuple[Platform, ...],
    ) -> AsyncIterator[dict[Platform, FixtureAdapter]]:
        del settings
        assert platforms == (Platform.BILIBILI,)
        yield {Platform.BILIBILI: adapter}

    monkeypatch.setattr(cli, "open_adapters", fake_open)


def test_collections_and_dry_run_are_read_only(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _install_adapter(monkeypatch, FixtureAdapter(("BV1",)))

    collections = runner.invoke(
        cli.app,
        ["collections", "bilibili", "--config", str(config), "--json"],
    )
    dry_run = runner.invoke(
        cli.app,
        ["sync", "bilibili", "--dry-run", "--config", str(config), "--json"],
    )

    assert collections.exit_code == 0, collections.output
    assert json.loads(collections.output)["collections"][0]["id"] == "fixture"
    assert dry_run.exit_code == 0, dry_run.output
    assert json.loads(dry_run.output)["dry_run"] is True
    assert not (tmp_path / "archive.db").exists()
    assert not (tmp_path / "vault").exists()


class ExpiredAdapter(FixtureAdapter):
    async def check_session(self) -> SessionStatus:
        return SessionStatus(state=SessionState.EXPIRED, diagnostic_code="fixture.expired")

    async def begin_login(self) -> LoginInstruction:
        return LoginInstruction(action=LoginAction.SCAN_QR, message="Scan in fixture UI.")


def test_login_reports_authenticated_and_user_action_states(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    _install_adapter(monkeypatch, FixtureAdapter(()))
    authenticated = runner.invoke(
        cli.app,
        ["login", "bilibili", "--config", str(config), "--json"],
    )
    _install_adapter(monkeypatch, ExpiredAdapter(()))
    expired = runner.invoke(
        cli.app,
        ["login", "bilibili", "--config", str(config), "--json"],
    )

    assert authenticated.exit_code == 0, authenticated.output
    assert json.loads(authenticated.output)["sessions"][0]["state"] == "authenticated"
    assert expired.exit_code == 3, expired.output
    assert json.loads(expired.output)["sessions"][0]["action"] == "scan_qr"


def test_metadata_sync_limits_items_and_separates_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    _install_adapter(monkeypatch, FixtureAdapter(("BV1", "BV2", "BV3"), page_size=2))

    result = runner.invoke(
        cli.app,
        [
            "sync",
            "bilibili",
            "--metadata-only",
            "--limit",
            "2",
            "--config",
            str(config),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["enumeration"]["references_observed"] == 2
    assert payload["enumeration"]["skeletons_rendered"] == 2
    assert payload["enumeration"]["limited"] is True
    assert payload["enumeration"]["complete"] is False
    assert payload["heavy"]["drain_requested"] is False
    assert payload["heavy"]["pending"] == 2
    with Database(tmp_path / "archive.db").connect() as connection:
        run = connection.execute(
            "SELECT enumeration_complete FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert run["enumeration_complete"] == 0


def test_foreground_mode_drains_jobs_with_safe_worker_hook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    _install_adapter(monkeypatch, FixtureAdapter(("BV1",)))

    async def complete_job(
        job: JobRecord,
        worker_id: str,
        resources: cli.CommandResources,
    ) -> None:
        resources.queue.complete(job.id, worker_id)

    monkeypatch.setattr(cli, "process_foreground_job", complete_job)
    result = runner.invoke(
        cli.app,
        [
            "sync",
            "bilibili",
            "--foreground",
            "--config",
            str(config),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["heavy"]["drain_requested"] is True
    assert payload["heavy"]["drained"] == 1
    assert payload["heavy"]["pending"] == 0


def test_default_foreground_pipeline_completes_assetless_fixture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    _install_adapter(monkeypatch, FixtureAdapter(("BV-local",)))

    result = runner.invoke(
        cli.app,
        [
            "sync",
            "bilibili",
            "--foreground",
            "--config",
            str(config),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["heavy"]["drained"] >= 1
    assert payload["heavy"]["failures"] == 0
    assert payload["heavy"]["pending"] == 0
    note = next((tmp_path / "vault").rglob("*.md"))
    assert "processing_status: complete" in note.read_text(encoding="utf-8")


class CancellingAdapter(FixtureAdapter):
    async def list_favorites(self, selected_collection, cursor=None):
        if cursor is not None:
            raise asyncio.CancelledError
        return await super().list_favorites(selected_collection, cursor)


def test_cancellation_checkpoints_cursor_releases_safely_and_never_reconciles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    _install_adapter(
        monkeypatch,
        CancellingAdapter(("BV1", "BV2", "BV3"), page_size=2),
    )

    result = runner.invoke(
        cli.app,
        [
            "sync",
            "bilibili",
            "--metadata-only",
            "--config",
            str(config),
            "--json",
        ],
    )

    assert result.exit_code == 130
    database = Database(tmp_path / "archive.db")
    with database.connect() as connection:
        run = connection.execute(
            "SELECT status, enumeration_complete, stats_json FROM runs LIMIT 1"
        ).fetchone()
        removed = connection.execute(
            "SELECT COUNT(*) FROM item_collections WHERE state = 'removed'"
        ).fetchone()[0]
        active_leases = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
        ).fetchone()[0]
    stats = json.loads(run["stats_json"])
    assert run["status"] == "cancelled"
    assert run["enumeration_complete"] == 0
    assert stats["checkpoint_cursor"] == "2"
    assert removed == 0
    assert active_leases == 0
