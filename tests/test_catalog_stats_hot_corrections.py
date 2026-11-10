"""Continuous terminal writes must not make a successfully repaired async day look abandoned."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update

from treg import timeutil
from treg.application import catalog_stats
from treg.domain.catalog import stats
from treg.domain.catalog import store as catalog_store
from treg.infra import catalog_stat_dirty
from treg.infra.catalog_observations import (
    EndpointObservationUnavailable, PostgresEndpointObservationReader, STALE_AFTER_S,
)
from treg.infra.db import session_maker
from treg.models import CallRecord, EndpointDayStat, EndpointStatDirty

BASE = datetime(2026, 10, 15, 8)
ENDPOINT = "test.async.hot_corrections"


@pytest.fixture
def hot_clock(clients, monkeypatch):
    clock = SimpleNamespace(now=BASE)
    monkeypatch.setattr(timeutil, "utcnow_naive", lambda: clock.now)
    monkeypatch.setattr(catalog_stats, "utcnow_naive", lambda: clock.now)
    monkeypatch.setattr(stats, "_now", lambda: clock.now)
    catalog = catalog_store.load()
    monkeypatch.setitem(catalog.by_id, ENDPOINT, {
        "id": ENDPOINT, "async": {"state": "status"}, "cost": {"type": "per_success"},
    })
    monkeypatch.setitem(catalog.adapters, ENDPOINT, object())
    return clock


async def _seed(day_age: int):
    submitted = BASE.replace(hour=0, minute=10) - timedelta(days=day_age)
    day = submitted.strftime("%Y-%m-%d")
    async with session_maker() as db:
        rows = [CallRecord(org_id=1, user_email="synthetic@example.test", tool_name=ENDPOINT,
                           method="POST", path="/synthetic", endpoint_id=ENDPOINT,
                           status_code=200, duration_ms=100, hit=True, verdict="valid",
                           cost_observed_micro=0, created_at=submitted + timedelta(seconds=n))
                for n in range(20)]
        db.add_all(rows)
        await db.commit()
        row_ids = [row.id for row in rows]
    await catalog_stats.refresh(session_maker, now=BASE)
    async with session_maker() as db:
        await catalog_stat_dirty.mark_dirty(db, [(ENDPOINT, day)], now=BASE)
        await db.commit()
    return day, row_ids


async def _marker(day):
    async with session_maker() as db:
        return await db.get(EndpointStatDirty, (ENDPOINT, day))


async def _bucket(day):
    async with session_maker() as db:
        return await db.get(EndpointDayStat, (ENDPOINT, day))


@contextmanager
def _during_recount(callback):
    """Use the real SQLite query and tally; inject a revision race on the same transaction.

    Cross-session PostgreSQL races are covered separately. Here the event makes acknowledgement
    observe a newer revision without substituting a fake source result or worker function.
    """
    engine = session_maker.kw["bind"].sync_engine

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if "from callrecord" in normalized and "callrecord.id <=" in normalized:
            callback(connection)

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)


@pytest.mark.parametrize("day_age", [0, 1], ids=["today", "yesterday"])
async def test_continuously_remarked_day_stays_readable_while_full_repairs_succeed(
    hot_clock, day_age,
):
    day, _ = await _seed(day_age)
    reader = PostgresEndpointObservationReader(session_maker)

    for minutes in (30, 60, 90, 120, 150):
        scan_started = BASE + timedelta(minutes=minutes)
        hot_clock.now = scan_started
        old_marker = await _marker(day)
        async with session_maker() as db:
            await catalog_stat_dirty.mark_dirty(db, [(ENDPOINT, day)], now=scan_started)
            await db.commit()
        assert (await _marker(day)).queued_at == old_marker.queued_at, "writes alone cannot renew age"

        def newer_revision(connection, scan_started=scan_started):
            connection.execute(update(EndpointStatDirty).where(
                EndpointStatDirty.endpoint_id == ENDPOINT, EndpointStatDirty.day == day
            ).values(revision=EndpointStatDirty.revision + 1))
            hot_clock.now = scan_started + timedelta(seconds=30)

        with _during_recount(newer_revision):
            # A long-running pass can have an older run timestamp than its source scan. Only
            # the actual scan start is a conservative lower bound on the remaining correction.
            result = await catalog_stats.refresh(session_maker, now=scan_started - timedelta(seconds=15))
        assert result["repaired"] == result["repair_pending"] == 1
        assert result["repair_failed"] == 0
        marker = await _marker(day)
        assert marker.revision == old_marker.revision + 2
        assert marker.queued_at == scan_started
        assert marker.queued_at != hot_clock.now  # finishing later must not claim a newer snapshot
        observation = (await reader.get_many([ENDPOINT]))[ENDPOINT]
        assert observation["samples"] == observation["hit_samples"] == 20
        assert observation["hit_rate"] == observation["ok_rate"] == 1.0

    assert hot_clock.now - BASE > timedelta(seconds=STALE_AFTER_S)
    last_scan = marker.queued_at
    hot_clock.now = last_scan + timedelta(seconds=STALE_AFTER_S + 1)
    async with session_maker() as db:
        await catalog_stat_dirty.mark_dirty(db, [(ENDPOINT, day)], now=hot_clock.now)
        await db.commit()
    # Keep the append cursor fresh, isolating stale terminal repairs from a dead whole worker.
    await catalog_stats.refresh(session_maker, max_dirty_buckets=0, now=hot_clock.now)
    assert (await _marker(day)).queued_at == last_scan
    with pytest.raises(EndpointObservationUnavailable) as caught:
        await reader.get_many([ENDPOINT])
    assert caught.value.completed == {}, "today's hot bucket has the same staleness guard"


@pytest.mark.parametrize("failure", ["source_scan", "after_acknowledgement"])
async def test_failed_repair_does_not_refresh_correction_age_or_replace_the_previous_bucket(
    hot_clock, monkeypatch, failure,
):
    day, ids = await _seed(0)
    async with session_maker() as db:
        row = await db.get(CallRecord, ids[0])
        row.hit, row.verdict = False, "not_found"
        db.add(row)
        await db.commit()
    before_bucket = (await _bucket(day)).model_dump()
    before_marker = await _marker(day)
    scan_started = BASE + timedelta(seconds=STALE_AFTER_S + 1)
    hot_clock.now = scan_started

    def newer_revision(connection):
        connection.execute(update(EndpointStatDirty).where(
            EndpointStatDirty.endpoint_id == ENDPOINT, EndpointStatDirty.day == day
        ).values(revision=EndpointStatDirty.revision + 1))
        hot_clock.now = scan_started + timedelta(seconds=30)
        if failure == "source_scan":
            raise TimeoutError("synthetic failure during source scan")

    original_acknowledge = catalog_stat_dirty.acknowledge

    async def fail_after_acknowledgement(db, *args, **kwargs):
        await original_acknowledge(db, *args, **kwargs)
        queued = (await db.execute(select(EndpointStatDirty.queued_at).where(
            EndpointStatDirty.endpoint_id == ENDPOINT, EndpointStatDirty.day == day))).scalar_one()
        assert queued == scan_started, "this test must fail after the transactional age update"
        raise TimeoutError("synthetic failure after acknowledgement")

    if failure == "after_acknowledgement":
        monkeypatch.setattr(catalog_stat_dirty, "acknowledge", fail_after_acknowledgement)
    with _during_recount(newer_revision):
        result = await catalog_stats.refresh(session_maker, now=scan_started)
    assert result["repaired"] == 0 and result["repair_failed"] == result["repair_pending"] == 1
    marker = await _marker(day)
    assert marker.queued_at == before_marker.queued_at == BASE
    assert marker.retry_at > scan_started
    assert (await _bucket(day)).model_dump() == before_bucket
    reader = PostgresEndpointObservationReader(session_maker)
    with pytest.raises(EndpointObservationUnavailable):
        await reader.get_many([ENDPOINT])

    monkeypatch.setattr(catalog_stat_dirty, "acknowledge", original_acknowledge)
    hot_clock.now = marker.retry_at + timedelta(seconds=1)
    recovered = await catalog_stats.refresh(session_maker, now=hot_clock.now)
    assert recovered["repaired"] == 1 and recovered["repair_pending"] == 0
    assert await _marker(day) is None
    observation = (await reader.get_many([ENDPOINT]))[ENDPOINT]
    assert observation["samples"] == observation["hit_samples"] == 20
    assert observation["hit_rate"] == 0.95
