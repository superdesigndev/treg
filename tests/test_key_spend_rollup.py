"""The spend rollup behind the Team page's "Spent" column and the Usage spend chart:
`treg-worker spend rollup`.

What these pin: that the rollup equals the ledger (per key plus the not-attributed line is the
ledger's settle total, and no other team's money is in it); that a re-run, an overlapping run or a
capped backfill never counts an entry twice; that an entry inside the lag waits; and that the
daily recount replaces what the fold got wrong instead of adding to it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from treg import crypto
from treg.application import key_spend_rollup as rollup
from treg.infra.db import session_maker
from treg.models import ApiKeySpendCursor, ApiKeySpendDay, CallRecord, LedgerEntry, Org

NOW = datetime(2026, 10, 9, 12)


def _entry(org_id: int, call_id: str | None, amount: int, at: datetime, *, kind: str = "settle",
           endpoint: str = "exa.search") -> LedgerEntry:
    return LedgerEntry(id=crypto.new_token()[:32], org_id=org_id, kind=kind, amount_micro=amount,
                       call_id=call_id, endpoint_id=endpoint or None, created_at=at)


def _record(org_id: int, call_ref: str, key_id: int | None, at: datetime) -> CallRecord:
    # A cost the ledger contradicts, so a total read from the audit table would be visibly wrong.
    return CallRecord(org_id=org_id, user_email="a@example.dev", tool_name="t", method="GET",
                      path="/", status_code=200, call_ref=call_ref, api_key_id=key_id,
                      cost_charged_micro=999_999, created_at=at)


async def _orgs(clients) -> tuple[int, int]:
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    async with session_maker() as db:
        other = Org(name="Other", slug="other-rollup")
        db.add(other)
        await db.commit()
        return org_id, other.id


async def _add(*rows) -> None:
    async with session_maker() as db:
        db.add_all(rows)
        await db.commit()


async def _rows(org_id: int) -> dict[tuple[str, int, str], tuple[int, int]]:
    async with session_maker() as db:
        got = (await db.execute(select(ApiKeySpendDay).where(ApiKeySpendDay.org_id == org_id))).scalars().all()
    return {(r.day, r.api_key_id, r.endpoint_id): (r.spent_micro, r.charges) for r in got}


async def _ledger_total(org_id: int, since: datetime) -> int:
    async with session_maker() as db:
        total = (await db.execute(select(func.sum(LedgerEntry.amount_micro)).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle",
            LedgerEntry.created_at >= since))).scalar_one()
    return -int(total or 0)


async def test_rollup_attributes_like_the_ledger_and_adds_up_to_it(clients):
    org_id, other = await _orgs(clients)
    day = datetime(2026, 10, 5, 9)
    await _add(
        _record(org_id, "c1", 1, day), _entry(org_id, "c1", -1_500, day, kind="reserve"),
        _entry(org_id, "c1", -1_000, day),
        # an overflow hold and a routed attempt settle under suffixed ids: the parent's row keys them
        _entry(org_id, "c1:overflow", -20, day), _entry(org_id, "c1:r1", -7, day, endpoint="tavily.search"),
        # two audit rows for one call still key it once
        _record(org_id, "c3", 2, day), _record(org_id, "c3", 2, day), _entry(org_id, "c3", -300, day),
        # not attributed: no audit row, an audit row without a key, no call id at all
        _entry(org_id, "lost", -50, day), _record(org_id, "keyless", None, day),
        _entry(org_id, "keyless", -5, day), _entry(org_id, None, -1, day, endpoint=""),
        # a zero settle moved no money and is no charge
        _record(org_id, "free", 1, day), _entry(org_id, "free", 0, day),
        # another team's audit row under a colliding ref never keys this team's charge, and its
        # own charge lands on its own rows
        _record(other, "lost", 9, day), _entry(other, "c1", -100_000, day),
        # older than the 31-day history: never folded
        _record(org_id, "old", 1, datetime(2026, 9, 1)), _entry(org_id, "old", -7_000, datetime(2026, 9, 1)),
        _entry(org_id, None, 1_000_000, day, kind="grant"),
    )

    result = await rollup.refresh(session_maker, now=NOW)

    assert result["caught_up"]
    assert await _rows(org_id) == {
        ("2026-10-05", 1, "exa.search"): (1_020, 2),
        ("2026-10-05", 1, "tavily.search"): (7, 1),
        ("2026-10-05", 2, "exa.search"): (300, 1),
        ("2026-10-05", 0, "exa.search"): (55, 2),
        ("2026-10-05", 0, ""): (1, 1),
    }
    assert sum(v[0] for v in (await _rows(org_id)).values()) == await _ledger_total(org_id, datetime(2026, 9, 9))
    assert await _rows(other) == {("2026-10-05", 0, "exa.search"): (100_000, 1)}
    async with session_maker() as db:
        cursor = await db.get(ApiKeySpendCursor, rollup.CURSOR_ID)
    assert cursor.history_from == "2026-09-09" and cursor.caught_up_at == NOW


async def test_a_rerun_and_a_capped_backfill_never_count_twice(clients):
    org_id, _ = await _orgs(clients)
    start = datetime(2026, 9, 20)
    entries = []
    for i in range(37):
        at = start + timedelta(hours=7 * i)
        entries += [_record(org_id, f"k{i}", 1 + i % 3, at), _entry(org_id, f"k{i}", -(100 + i), at)]
    # Several entries on one instant: the (created_at, id) watermark must not skip or repeat them.
    tie = datetime(2026, 10, 1, 8)
    entries += [_entry(org_id, f"tie{i}", -1_000, tie) for i in range(6)]
    await _add(*entries)

    runs = 0
    while True:
        runs += 1
        result = await rollup.refresh(session_maker, now=NOW, max_rows=5, batch_rows=2)
        if result["caught_up"]:
            break
    assert runs > 5      # the first backfill took several capped runs
    again = await rollup.refresh(session_maker, now=NOW)
    assert again["rows"] == 0

    rows = await _rows(org_id)
    assert sum(v[0] for v in rows.values()) == await _ledger_total(org_id, start)
    assert sum(v[1] for v in rows.values()) == 43


async def test_an_entry_inside_the_lag_waits_for_the_next_run(clients):
    org_id, _ = await _orgs(clients)
    await _add(_entry(org_id, "a", -10, NOW - timedelta(minutes=10)),
               _entry(org_id, "b", -20, NOW - timedelta(minutes=1)))
    first = await rollup.refresh(session_maker, now=NOW)
    assert first["rows"] == 1
    assert sum(v[0] for v in (await _rows(org_id)).values()) == 10
    later = await rollup.refresh(session_maker, now=NOW + rollup.LAG)
    assert later["rows"] == 1
    assert sum(v[0] for v in (await _rows(org_id)).values()) == 30


async def test_the_daily_recount_replaces_what_the_fold_got_wrong(clients, caplog):
    org_id, _ = await _orgs(clients)
    day = datetime(2026, 10, 8, 10)
    await _add(_record(org_id, "a", 1, day), _entry(org_id, "a", -100, day),
               _entry(org_id, "b", -40, day))           # its audit row is not there yet
    await rollup.refresh(session_maker, now=day + timedelta(hours=1))
    assert await _rows(org_id) == {("2026-10-08", 1, "exa.search"): (100, 1),
                                   ("2026-10-08", 0, "exa.search"): (40, 1)}

    # Behind the fold's back: the late audit row now keys "b", and a settle committed after the
    # watermark had passed its timestamp.
    late = day - timedelta(hours=1)
    await _add(_record(org_id, "b", 2, day), _entry(org_id, "c", -5, late))
    caplog.set_level(logging.WARNING, logger="treg.spend")
    next_day = datetime(2026, 10, 9, 1)
    result = await rollup.refresh(session_maker, now=next_day)

    assert result["recounted"] == 1
    assert await _rows(org_id) == {("2026-10-08", 1, "exa.search"): (100, 1),
                                   ("2026-10-08", 2, "exa.search"): (40, 1),
                                   ("2026-10-08", 0, "exa.search"): (5, 1)}
    assert "recount differs" in caplog.text
    # Once a day: a second run that day does not recount, and nothing is added twice. (The next
    # day's recount still covers the 8th, so this late entry is repaired then.)
    await _add(_entry(org_id, "d", -1, late))
    again = await rollup.refresh(session_maker, now=next_day + timedelta(hours=1))
    assert again["recounted"] == 0
    assert sum(v[0] for v in (await _rows(org_id)).values()) == 145
    await rollup.refresh(session_maker, now=next_day + timedelta(days=1))
    assert sum(v[0] for v in (await _rows(org_id)).values()) == 146


async def _three_teams(clients) -> list[int]:
    org_id, other = await _orgs(clients)
    async with session_maker() as db:
        third = Org(name="Third", slug="third-rollup")
        db.add(third)
        await db.commit()
        orgs = [org_id, other, third.id]
    day = datetime(2026, 10, 8, 10)
    await _add(*[_entry(o, f"{o}-a", -100, day) for o in orgs])
    await rollup.refresh(session_maker, now=day + timedelta(hours=1))
    # Behind the fold's back, one late charge per team: only a recount picks it up.
    await _add(*[_entry(o, f"{o}-late", -5, day - timedelta(hours=1)) for o in orgs])
    return orgs


async def test_an_overlapping_run_never_starts_the_same_recount(clients, monkeypatch):
    orgs = await _three_teams(clients)
    next_day = datetime(2026, 10, 9, 1)
    real = rollup._recount_team
    calls: list[int] = []
    overlapped: list[dict] = []

    async def slow_team(session_factory, org_id, *args, **kw):
        calls.append(org_id)
        if len(calls) == 1:
            # The next cron fires while this run is still on its first team.
            overlapped.append(await rollup.refresh(session_maker, now=next_day + timedelta(minutes=5)))
        return await real(session_factory, org_id, *args, **kw)
    monkeypatch.setattr(rollup, "_recount_team", slow_team)

    first = await rollup.refresh(session_maker, now=next_day)

    assert overlapped[0]["recounted"] == 0
    assert calls == orgs                     # each team once: the overlapping run started none
    assert first["recounted"] == 3
    for org_id in orgs:
        assert sum(v[0] for v in (await _rows(org_id)).values()) == 105
    async with session_maker() as db:
        cursor = await db.get(ApiKeySpendCursor, rollup.CURSOR_ID)
    assert cursor.recounted_on == "2026-10-09" and cursor.recount_owner is None


async def test_a_capped_recount_resumes_where_the_last_run_stopped(clients, monkeypatch):
    orgs = await _three_teams(clients)
    next_day = datetime(2026, 10, 9, 1)
    calls: list[int] = []
    real = rollup._recount_team

    async def counted(session_factory, org_id, *args, **kw):
        calls.append(org_id)
        return await real(session_factory, org_id, *args, **kw)
    monkeypatch.setattr(rollup, "_recount_team", counted)

    for minute in range(3):   # no time to spare: one team per run, then the lease is handed back
        await rollup.refresh(session_maker, now=next_day + timedelta(minutes=5 * minute), max_recount_seconds=0)
        async with session_maker() as db:
            cursor = await db.get(ApiKeySpendCursor, rollup.CURSOR_ID)
        assert cursor.recount_owner is None
        assert (cursor.recounted_on == "2026-10-09") is (minute == 2)
    assert calls == orgs
    again = await rollup.refresh(session_maker, now=next_day + timedelta(hours=1))
    assert again["recounted"] == 0 and calls == orgs
    for org_id in orgs:
        assert sum(v[0] for v in (await _rows(org_id)).values()) == 105


async def test_a_dead_runs_recount_lease_expires(clients):
    orgs = await _three_teams(clients)
    next_day = datetime(2026, 10, 9, 1)
    async with session_maker() as db:
        cursor = await db.get(ApiKeySpendCursor, rollup.CURSOR_ID)
        cursor.recount_day, cursor.recount_owner = "2026-10-09", "dead-run"
        cursor.recount_lease_until = next_day + timedelta(minutes=10)
        db.add(cursor)
        await db.commit()
    assert (await rollup.refresh(session_maker, now=next_day))["recounted"] == 0
    assert (await rollup.refresh(session_maker, now=next_day + timedelta(minutes=11)))["recounted"] == 3
    for org_id in orgs:
        assert sum(v[0] for v in (await _rows(org_id)).values()) == 105


async def test_the_watermark_bound_selects_exactly_the_entries_after_it(clients):
    """`_after` is the index-friendly form of "(created_at, id) > watermark": the same rows as the
    old OR form, including every entry that shares the watermark's instant. Both run in the
    database, whose collation orders the ids."""
    import random
    from sqlalchemy import and_, or_, select as sql_select

    org_id, _ = await _orgs(clients)
    rnd = random.Random(3)
    instants = [datetime(2026, 10, 1, 8) + timedelta(seconds=rnd.randint(0, 5)) for _ in range(60)]
    await _add(*[_entry(org_id, f"w{i}", -1, at) for i, at in enumerate(instants)])
    async with session_maker() as db:
        def ids(clause):
            return sql_select(LedgerEntry.id).where(*clause).order_by(LedgerEntry.created_at, LedgerEntry.id)
        ordered = (await db.execute(sql_select(LedgerEntry.created_at, LedgerEntry.id)
                                    .order_by(LedgerEntry.created_at, LedgerEntry.id))).all()
        for at, entry_id in [ordered[0], ordered[17], ordered[30], ordered[-1], (datetime(2026, 10, 1, 8, 0, 2), "")]:
            old = [or_(LedgerEntry.created_at > at, and_(LedgerEntry.created_at == at, LedgerEntry.id > entry_id))]
            want = (await db.execute(ids(old))).scalars().all()
            got = (await db.execute(ids(rollup._after(at, entry_id)))).scalars().all()
            assert got == want
