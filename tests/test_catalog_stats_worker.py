"""The scheduled `treg-worker catalog stats` command folds a bounded batch per run."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from treg import worker
from treg.infra.db import session_maker
from treg.models import CallRecord

ASYNC = "wiza.people.email.find"


def _run(capsys, *argv):
    code = worker.main(["catalog", *argv])
    output = capsys.readouterr()
    return code, output.out, output.err




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
