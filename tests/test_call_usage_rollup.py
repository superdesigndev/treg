"""The call usage rollup behind the Usage page's counts and the observed-agents roster:
`treg-worker usage rollup`.

What these pin: that the reports read from the rollup answer exactly what the live audit-table
aggregate answered (the rows after the cursor included, nothing counted twice); that a re-run or a
capped backfill never folds a row twice; that a row inside the lag waits; that the reports say the
history is being prepared until the first backfill catches up, and name where it begins.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import event, func, select

from treg.application import call_usage_rollup as rollup
from treg.routers import orgs
from treg.infra.db import _engine as engine, session_maker
from treg.models import CallRecord, CallUsageCursor, CallUsageDay, Org, RunRecord
from treg.timeutil import utcnow_naive


def _call(org_id: int, email: str, tool: str, at, *, kind: str = "call", client: str = "claude-code") -> CallRecord:
    return CallRecord(org_id=org_id, user_email=email, tool_name=tool, method="GET", path="/",
                      status_code=200, kind=kind, client=client, created_at=at)


async def _add(*rows) -> None:
    async with session_maker() as db:
        db.add_all(rows)
        await db.commit()


async def _in_history() -> int:
    start = utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=rollup.HISTORY_DAYS - 1)
    async with session_maker() as db:
        return int((await db.execute(select(func.count()).select_from(CallRecord).where(
            CallRecord.org_id.is_not(None), CallRecord.created_at >= start))).scalar_one())


async def _folded(org_id: int | None = None) -> int:
    async with session_maker() as db:
        q = select(func.coalesce(func.sum(CallUsageDay.calls), 0))
        if org_id is not None:
            q = q.where(CallUsageDay.org_id == org_id)
        return int((await db.execute(q)).scalar_one())


async def _team(clients) -> tuple[int, int]:
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    async with session_maker() as db:
        other = Org(name="Other", slug="other-usage-rollup")
        db.add(other)
        await db.commit()
        return org_id, other.id


async def _seed(org_id: int, other_id: int) -> None:
    now = utcnow_naive()
    await _add(
        _call(org_id, "a@example.dev", "exa.search", now - timedelta(minutes=5)),
        _call(org_id, "a@example.dev", "exa.search", now - timedelta(days=1)),
        _call(org_id, "a@example.dev", "gh", now - timedelta(minutes=5), kind="local_run", client="cli"),
        _call(org_id, "b@example.dev", "tavily.search", now - timedelta(days=3), client="cursor"),
        _call(org_id, "b@example.dev", "exa.search", now - timedelta(minutes=5), kind="odd", client=""),
        _call(org_id, "a@example.dev", "exa.search", now - timedelta(days=40)),   # before the history
        _call(other_id, "a@example.dev", "exa.search", now - timedelta(minutes=5)),
        RunRecord(org_id=org_id, user_email="b@example.dev", bundle_name="gh", argv=["x"], exit_code=0,
                  duration_ms=5, client="cursor", created_at=now - timedelta(days=1)),
    )


async def _reports(clients, org_id: int) -> tuple[dict, list]:
    usage = (await clients.get(f"/orgs/{org_id}/usage?days=30")).json()
    observed = (await clients.get(f"/orgs/{org_id}/agents/observed")).json()
    usage.pop("history_from", None)
    return usage, observed


async def test_reports_from_the_rollup_equal_the_live_aggregate(clients):
    org_id, other_id = await _team(clients)
    await _seed(org_id, other_id)
    live_usage, live_observed = await _reports(clients, org_id)   # no cursor yet: the live aggregate
    assert live_usage["totals"]["total"] >= 5

    first = await rollup.refresh()
    assert first["caught_up"] is True
    calls = live_usage["totals"]["call"] + live_usage["totals"]["local_run"]
    assert await _folded(org_id) == calls and await _folded(other_id) == 1   # the 40-day-old row is outside

    # Rows the worker has not reached yet are read after the cursor, not lost and not doubled.
    now = utcnow_naive()
    await _add(_call(org_id, "a@example.dev", "exa.search", now - timedelta(seconds=5)),
               _call(org_id, "c@example.dev", "gh", now - timedelta(seconds=5), client="codex"))
    live_usage["totals"]["call"] += 2
    live_usage["totals"]["total"] += 2
    usage, observed = await _reports(clients, org_id)
    assert usage["ready"] is True
    assert usage["totals"] == live_usage["totals"]
    assert sorted(u["user_email"] for u in usage["by_user"]) == ["a@example.dev", "b@example.dev", "c@example.dev"]
    tools = {t["name"]: t["total"] for t in live_usage["by_tool"]}
    tools["exa.search"] += 1
    tools["gh"] += 1
    assert {t["name"]: t["total"] for t in usage["by_tool"]} == tools
    assert sum(d["total"] for d in usage["by_day"]) == live_usage["totals"]["total"]
    live = {(r["member"], r["client"]): r for r in live_observed}
    got = {(r["member"], r["client"]): r for r in observed}
    assert set(got) == set(live) | {("c@example.dev", "codex")}
    for pair, was in live.items():
        extra = 1 if pair == ("a@example.dev", "claude-code") else 0
        assert got[pair]["calls_30d"] == was["calls_30d"] + extra
        assert got[pair]["used_today"] == was["used_today"] + extra
        if not extra:
            assert got[pair]["last_seen"] == was["last_seen"]


async def test_a_rerun_or_a_capped_backfill_never_folds_a_row_twice(clients):
    org_id, other_id = await _team(clients)
    await _seed(org_id, other_id)
    capped = await rollup.refresh(max_rows=2, batch_rows=1)
    assert capped["caught_up"] is False and capped["rows"] == 2 and await _folded() <= 2
    while not (await rollup.refresh(max_rows=2, batch_rows=1))["caught_up"]:
        pass
    assert await _folded() == await _in_history()
    await rollup.refresh()
    assert await _folded() == await _in_history()


async def test_a_row_inside_the_lag_waits_for_the_next_run(clients):
    org_id, _ = await _team(clients)
    now = utcnow_naive()
    await _add(_call(org_id, "a@example.dev", "t", now - timedelta(minutes=5)),
               _call(org_id, "a@example.dev", "t", now - timedelta(seconds=5)),
               _call(org_id, "a@example.dev", "t", now - timedelta(minutes=5)))   # older, but a later id
    got = await rollup.refresh(now=now)
    assert got["rows"] == 1 and got["caught_up"] is True
    assert await _folded(org_id) == 1
    await rollup.refresh(now=now + timedelta(minutes=2))
    assert await _folded(org_id) == 3


async def test_reports_wait_for_the_first_backfill_and_name_where_history_begins(clients):
    org_id, _ = await _team(clients)
    await _add(_call(org_id, "a@example.dev", "t", utcnow_naive() - timedelta(minutes=5)))
    day = (utcnow_naive() - timedelta(days=rollup.HISTORY_DAYS - 1)).strftime("%Y-%m-%d")
    await _add(CallUsageCursor(id=rollup.CURSOR_ID, cursor_id=0, history_from=day))

    usage = (await clients.get(f"/orgs/{org_id}/usage?days=90")).json()
    assert usage["ready"] is False and usage["history_from"] == day and "totals" not in usage
    assert (await clients.get(f"/orgs/{org_id}/agents/observed")).json() == []

    await rollup.refresh()
    usage = (await clients.get(f"/orgs/{org_id}/usage?days=90")).json()
    assert usage["ready"] is True and usage["history_from"] == day and usage["days"] == 90
    assert usage["totals"]["call"] >= 1


async def test_a_new_cursor_starts_a_new_history_instead_of_adding_to_the_old_one(clients):
    org_id, other_id = await _team(clients)
    await _seed(org_id, other_id)
    await rollup.refresh()
    async with session_maker() as db:    # the documented fallback to the live aggregate
        await db.delete(await db.get(CallUsageCursor, rollup.CURSOR_ID))
        await db.commit()
    await rollup.refresh()
    assert await _folded() == await _in_history()


async def test_overlapping_runs_never_fold_the_same_rows_twice(clients):
    org_id, _ = await _team(clients)
    await rollup.refresh()                 # the cursor exists; the race is over the next rows
    at = utcnow_naive() - timedelta(minutes=5)
    await _add(*(_call(org_id, "a@example.dev", "t", at) for _ in range(100)))
    later = utcnow_naive() + timedelta(minutes=2)   # past the lag for every row, the fixture's too
    await asyncio.gather(rollup.refresh(batch_rows=10, now=later), rollup.refresh(batch_rows=10, now=later))
    assert await _folded() == await _in_history()


async def _used_today(clients, org_id: int, email: str) -> dict[str, int]:
    """Today's count for `email` as the members list and the agents list each report it."""
    members = (await clients.get(f"/orgs/{org_id}/members")).json()
    return {m["email"]: m["used_today"] for m in members}[email]


async def test_todays_roster_counts_read_the_rollup_and_its_tail_once_caught_up(clients, monkeypatch):
    me = (await clients.get("/auth/me")).json()
    org_id, email = me["org_id"], me["email"]
    midnight = utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    now = midnight + timedelta(hours=12)   # every seeded "today" row stays inside one UTC day
    monkeypatch.setattr(orgs, "_day_start_utc", lambda: midnight)
    await _add(*(_call(org_id, email, "t", now - timedelta(minutes=5)) for _ in range(3)),
               _call(org_id, email, "t", now - timedelta(days=2)),
               _call(org_id, "someone-else@example.dev", "t", now - timedelta(minutes=5)))
    live = await _used_today(clients, org_id, email)   # no cursor yet: the audit table itself
    assert live >= 3

    await rollup.refresh(now=now + timedelta(minutes=2))
    await _add(_call(org_id, email, "t", now - timedelta(seconds=5)))   # after the cursor
    statements: list[str] = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        if "callrecord" in statement.lower():
            statements.append(statement.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        got = await _used_today(clients, org_id, email)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert got == live + 1
    grouped = [sql for sql in statements if "group by" in sql]
    assert grouped and all("callusagecursor" in sql for sql in grouped), statements
