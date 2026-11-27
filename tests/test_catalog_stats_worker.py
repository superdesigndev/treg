"""The scheduled `treg-worker catalog stats` command folds a bounded batch per run."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import timedelta

from conftest import seed_teams
from treg import worker
from treg.infra.db import session_maker
from treg.models import CallRecord
from treg.timeutil import utcnow_naive

ASYNC = "wiza.people.email.find"


async def _stats_run(capsys, max_rows: int) -> dict:
    """One run of the command, on the test's event loop (asyncpg connections belong to one loop)."""
    args = Namespace(max_rows=max_rows, max_dirty_buckets=0, max_repair_seconds=60.0,
                     bucket_timeout_seconds=10.0)
    assert await worker._catalog_stats(args) == 0
    return json.loads(capsys.readouterr().out)


async def test_scheduled_stats_folds_at_most_max_rows_per_run(clients, capsys):
    await seed_teams(1)
    async with session_maker() as db:
        for _ in range(3):
            db.add(CallRecord(org_id=1, user_email="synthetic@example.test", tool_name=ASYNC,
                              method="POST", path="/synthetic", endpoint_id=ASYNC, status_code=200,
                              duration_ms=100, created_at=utcnow_naive() - timedelta(hours=2)))
        await db.commit()
    assert (await _stats_run(capsys, max_rows=2))["rows"] == 2
    assert (await _stats_run(capsys, max_rows=2))["rows"] == 1
