"""The one-off repair CLI keeps preview, apply, status and scheduled folding distinct."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from treg import worker
from treg.application import catalog_stats
from treg.infra import db


@pytest.fixture
def commands(monkeypatch):
    calls = SimpleNamespace(
        verify=AsyncMock(), requeue=AsyncMock(), status=AsyncMock(), refresh=AsyncMock(),
    )
    monkeypatch.setattr(db, "verify_db", calls.verify)
    monkeypatch.setattr(catalog_stats, "requeue_async_stats", calls.requeue)
    monkeypatch.setattr(catalog_stats, "repair_status", calls.status)
    monkeypatch.setattr(catalog_stats, "refresh", calls.refresh)
    monkeypatch.setattr(worker, "_need_server", lambda: None)

    async def dispatch(args):
        return await args.fn(args)

    # Exercise the real parser and handlers without worker telemetry or external stores.
    monkeypatch.setattr(worker, "_run_command", dispatch)
    return calls


def _manifest(*, applied=True):
    return {
        "kind": "catalog-async-repair", "version": 1, "applied": applied,
        "requested_at": "2026-10-15T12:00:00", "window_start": "2026-09-15",
        "window_end": "2026-10-15", "target_count": 1,
        "targets": [{"endpoint_id": "wiza.people.email.find", "day": "2026-10-14"}],
    }


@pytest.mark.parametrize("apply", [False, True], ids=["default-preview", "explicit-apply"])
def test_requeue_defaults_to_preview_and_only_applies_when_requested(commands, capsys, apply):
    manifest = _manifest(applied=apply)
    commands.requeue.return_value = manifest

    assert worker.main(["catalog", "requeue-async-stats", *(["--apply"] if apply else [])]) == 0

    commands.verify.assert_awaited_once_with()
    commands.requeue.assert_awaited_once_with(apply=apply)
    commands.status.assert_not_awaited()
    commands.refresh.assert_not_awaited()
    output = capsys.readouterr()
    assert json.loads(output.out) == manifest
    assert output.err == ""


@pytest.mark.parametrize("complete,exit_code", [(True, 0), (False, 1)], ids=["complete", "pending"])
def test_status_reads_the_saved_manifest_and_reports_json_and_completion_exit_code(
    commands, capsys, tmp_path, complete, exit_code,
):
    manifest = _manifest()
    path = tmp_path / "saved-applied-targets.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = {"complete": complete, "repaired": int(complete), "pending": int(not complete),
              "missing": 0, "aged_out": 0, "targets": []}
    commands.status.return_value = result

    assert worker.main(["catalog", "repair-status", "--manifest", str(path)]) == exit_code

    commands.verify.assert_awaited_once_with()
    commands.status.assert_awaited_once_with(manifest)
    commands.requeue.assert_not_awaited()
    commands.refresh.assert_not_awaited()
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert output.err == ""


def test_status_requires_an_explicit_manifest_before_starting_database_work(commands, capsys):
    with pytest.raises(SystemExit) as caught:
        worker.main(["catalog", "repair-status"])
    assert caught.value.code == 2
    commands.verify.assert_not_awaited()
    commands.status.assert_not_awaited()
    commands.requeue.assert_not_awaited()
    commands.refresh.assert_not_awaited()
    output = capsys.readouterr()
    assert output.out == "" and "--manifest" in output.err


@pytest.mark.parametrize("contents", [None, "{broken json"], ids=["missing-file", "malformed-json"])
def test_unreadable_or_malformed_manifest_is_invalid_without_database_work(
    commands, capsys, tmp_path, contents,
):
    path = tmp_path / "manifest.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    assert worker.main(["catalog", "repair-status", "--manifest", str(path)]) == 2

    commands.verify.assert_not_awaited()
    commands.status.assert_not_awaited()
    commands.requeue.assert_not_awaited()
    commands.refresh.assert_not_awaited()
    output = capsys.readouterr()
    assert output.out == "" and output.err.strip()


def test_status_rejects_an_unapplied_manifest_with_exit_two(commands, capsys, tmp_path):
    manifest = _manifest(applied=False)
    path = tmp_path / "preview.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    commands.status.side_effect = ValueError("repair status requires an applied manifest")

    assert worker.main(["catalog", "repair-status", "--manifest", str(path)]) == 2

    commands.status.assert_awaited_once_with(manifest)
    commands.requeue.assert_not_awaited()
    commands.refresh.assert_not_awaited()
    output = capsys.readouterr()
    assert output.out == ""
    assert "requires an applied manifest" in output.err


def test_requeue_validation_failure_is_exit_two_without_running_the_cron(commands, capsys):
    commands.requeue.side_effect = ValueError("catalog stats cursor missing")

    assert worker.main(["catalog", "requeue-async-stats", "--apply"]) == 2

    commands.verify.assert_awaited_once_with()
    commands.requeue.assert_awaited_once_with(apply=True)
    commands.status.assert_not_awaited()
    commands.refresh.assert_not_awaited()
    output = capsys.readouterr()
    assert output.out == "" and "cursor missing" in output.err


def test_scheduled_stats_keeps_its_own_limits_and_does_not_requeue_or_check_status(commands, capsys):
    result = {"rows": 7, "repaired": 1, "repair_pending": 2}
    commands.refresh.return_value = result

    assert worker.main([
        "catalog", "stats", "--max-rows", "7", "--max-dirty-buckets", "3",
        "--max-repair-seconds", "4", "--bucket-timeout-seconds", "2",
    ]) == 0

    commands.verify.assert_awaited_once_with()
    commands.refresh.assert_awaited_once_with(
        max_rows=7, max_dirty_buckets=3, max_repair_seconds=4.0, bucket_timeout_s=2.0,
    )
    commands.requeue.assert_not_awaited()
    commands.status.assert_not_awaited()
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert output.err == ""
