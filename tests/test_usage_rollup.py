"""The Usage page's counts: `GET /orgs/{id}/usage`.

What these pin: by member, by kind, by tool and by day say what they always said (a run's tool
merges with a call's tool of the same name; an unexpected kind counts as a call; another team's
rows and rows before the window are not counted), and the counts read the call audit table twice,
not once per section: one grouped pass for member, kind and tool, one for the days.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import event

from treg.infra import db as infra_db
from treg.infra.db import session_maker
from treg.models import CallRecord, Org, RunRecord
from treg.timeutil import utcnow_naive


def _call(org_id: int, email: str, tool: str, at, kind: str = "call") -> CallRecord:
    return CallRecord(org_id=org_id, user_email=email, tool_name=tool, method="GET", path="/",
                      status_code=200, kind=kind, created_at=at)


def _run(org_id: int, email: str, tool: str, at) -> RunRecord:
    return RunRecord(org_id=org_id, user_email=email, bundle_name=tool, argv=["x"], exit_code=0,
                     duration_ms=5, created_at=at)


@contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement.lower())
    engines = list({id(e.sync_engine): e.sync_engine for e in infra_db._engines}.values())
    for engine in engines:
        event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record)


async def test_usage_counts_are_unchanged_and_read_the_audit_table_twice(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    now = utcnow_naive()
    today, yesterday = now - timedelta(minutes=5), now - timedelta(days=1)
    async with session_maker() as db:
        other = Org(name="Other", slug="other-usage")
        db.add(other)
        await db.flush()
        db.add_all([
            _call(org_id, "a@example.dev", "exa.search", today),
            _call(org_id, "a@example.dev", "exa.search", yesterday),
            _call(org_id, "a@example.dev", "gh", today, kind="local_run"),
            _call(org_id, "b@example.dev", "tavily.search", today),
            _call(org_id, "b@example.dev", "exa.search", today, kind="odd"),     # counts as a call
            _run(org_id, "b@example.dev", "gh", yesterday),                       # same tool name as a call
            _call(org_id, "a@example.dev", "exa.search", now - timedelta(days=40)),  # before the window
            _call(other.id, "a@example.dev", "exa.search", today),                # another team's row
        ])
        await db.commit()

    with _statements() as seen:
        got = (await clients.get(f"/orgs/{org_id}/usage?days=30")).json()

    assert got["totals"] == {"call": 4, "local_run": 1, "server_run": 1, "total": 6}
    assert sorted(got["by_user"], key=lambda u: u["user_email"]) == [
        {"user_email": "a@example.dev", "call": 2, "local_run": 1, "server_run": 0, "total": 3},
        {"user_email": "b@example.dev", "call": 2, "local_run": 0, "server_run": 1, "total": 3},
    ]
    assert got["by_tool"] == [{"name": "exa.search", "total": 3}, {"name": "gh", "total": 2},
                              {"name": "tavily.search", "total": 1}]
    assert [d["total"] for d in got["by_day"]] == [2, 4]
    assert len([s for s in seen if "from callrecord" in s]) == 2
