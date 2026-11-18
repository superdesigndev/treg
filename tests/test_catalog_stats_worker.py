"""The one-off repair CLI keeps preview, apply, status and scheduled folding distinct."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from treg import worker
from treg.infra.db import session_maker
from treg.models import CallRecord
from test_catalog_stats_requeue import ASYNC, _buckets, _markers, _seed_buckets

# The commands read the real clock; yesterday is inside the window whatever the hour.
DAY = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


def _run(capsys, *argv):
    code = worker.main(["catalog", *argv])
    output = capsys.readouterr()
    return code, output.out, output.err


def _reconciled():
    return [row["terminal_reconciled"] for row in asyncio.run(_buckets()) if row["endpoint_id"] == ASYNC]


def test_requeue_previews_by_default_and_changes_buckets_only_with_apply(clients, capsys):
    asyncio.run(_seed_buckets([(ASYNC, DAY)]))

    code, out, err = _run(capsys, "requeue-async-stats")
    assert code == 0
    preview = json.loads(out)
    assert preview["applied"] is False
    assert {"endpoint_id": ASYNC, "day": DAY} in preview["targets"]
    assert _reconciled() == [True] and asyncio.run(_markers()) == []

    code, out, err = _run(capsys, "requeue-async-stats", "--apply")
    assert code == 0
    applied = json.loads(out)
    assert applied["applied"] is True and applied["targets"] == preview["targets"]
    assert _reconciled() == [False]
    assert [(m["endpoint_id"], m["day"]) for m in asyncio.run(_markers())] == [(ASYNC, DAY)]


def test_status_is_pending_until_scheduled_stats_repairs_the_applied_targets(clients, capsys, tmp_path):
    asyncio.run(_seed_buckets([(ASYNC, DAY)]))
    code, out, _ = _run(capsys, "requeue-async-stats", "--apply")
    manifest = tmp_path / "applied.json"
    manifest.write_text(out, encoding="utf-8")

    code, out, err = _run(capsys, "repair-status", "--manifest", str(manifest))
    assert code == 1
    assert json.loads(out)["complete"] is False and json.loads(out)["pending"] == 1

    code, out, err = _run(capsys, "stats")
    assert code == 0
    assert json.loads(out)["repaired"] == 1

    code, out, err = _run(capsys, "repair-status", "--manifest", str(manifest))
    assert code == 0
    assert json.loads(out)["complete"] is True and json.loads(out)["repaired"] == 1


def test_status_requires_an_explicit_manifest(clients, capsys):
    with pytest.raises(SystemExit) as caught:
        worker.main(["catalog", "repair-status"])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert output.out == "" and "--manifest" in output.err


@pytest.mark.parametrize("contents", [None, "{broken json"], ids=["missing-file", "malformed-json"])
def test_unreadable_or_malformed_manifest_is_invalid(clients, capsys, tmp_path, contents):
    path = tmp_path / "manifest.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    code, out, err = _run(capsys, "repair-status", "--manifest", str(path))
    assert code == 2 and out == "" and err.strip()


def test_status_rejects_a_preview_manifest(clients, capsys, tmp_path):
    asyncio.run(_seed_buckets([(ASYNC, DAY)]))
    _, out, _ = _run(capsys, "requeue-async-stats")
    path = tmp_path / "preview.json"
    path.write_text(out, encoding="utf-8")
    code, out, err = _run(capsys, "repair-status", "--manifest", str(path))
    assert code == 2 and out == ""
    assert "requires an applied" in err


def test_apply_without_the_stats_cursor_is_refused_and_writes_nothing(clients, capsys):
    asyncio.run(_seed_buckets([(ASYNC, DAY)], cursor=False))
    code, out, err = _run(capsys, "requeue-async-stats", "--apply")
    assert code == 2 and out == ""
    assert "cursor missing" in err
    assert _reconciled() == [True] and asyncio.run(_markers()) == []


def test_scheduled_stats_folds_at_most_max_rows_per_run(clients, capsys):
    async def audit_rows(n):
        async with session_maker() as db:
            for _ in range(n):
                db.add(CallRecord(org_id=1, user_email="synthetic@example.test", tool_name=ASYNC,
                                  method="POST", path="/synthetic", endpoint_id=ASYNC, status_code=200,
                                  duration_ms=100, created_at=datetime.utcnow() - timedelta(hours=2)))
            await db.commit()

    asyncio.run(audit_rows(3))
    code, out, err = _run(capsys, "stats", "--max-rows", "2", "--max-dirty-buckets", "0")
    assert code == 0
    assert json.loads(out)["rows"] == 2
    code, out, _ = _run(capsys, "stats", "--max-rows", "2", "--max-dirty-buckets", "0")
    assert json.loads(out)["rows"] == 1
