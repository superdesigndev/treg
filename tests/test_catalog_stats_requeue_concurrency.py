"""A one-off recount cannot be acknowledged by a repair that acquired the cursor first."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select, text

from treg.application import catalog_stats
from treg.domain.catalog import store as catalog_store
from treg.infra.catalog_stat_dirty import mark_dirty
from treg.infra.db import reset_db, session_maker
from treg.models import CallRecord, EndpointDayStat, EndpointStatDirty

NOW = datetime(2026, 10, 15, 12)
EARLIER = NOW - timedelta(minutes=5)
SUBMITTED = datetime(2026, 10, 14, 23)
DAY = "2026-10-14"
EP = "wiza.people.email.find"


@pytest.fixture
async def postgres_catalog_requeue(monkeypatch):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await reset_db()
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: False)
    return engine


async def _seed_dirty_bucket():
    async with session_maker() as db:
        row = CallRecord(user_email="synthetic@example.test", tool_name=EP, method="POST",
                         path="/synthetic", endpoint_id=EP, status_code=200, duration_ms=100,
                         hit=None, cost_observed_micro=0, created_at=SUBMITTED)
        db.add(row)
        await db.commit()
        row_id = row.id
    await catalog_stats.refresh(session_maker, now=EARLIER)
    async with session_maker() as db:
        row = await db.get(CallRecord, row_id)
        row.hit, row.verdict = True, "valid"
        db.add(row)
        await mark_dirty(db, [(EP, DAY)], now=EARLIER)
        await db.commit()


async def _wait_until_blocked(waiter_pid, blocker_pid):
    async with asyncio.timeout(5):
        async with session_maker() as observer:
            while True:
                blockers = (await observer.execute(text("SELECT pg_blocking_pids(:pid)"),
                                                   {"pid": waiter_pid})).scalar_one()
                if blocker_pid in blockers:
                    return
                await asyncio.sleep(0.01)


async def test_apply_waits_for_older_repair_then_requires_a_new_reconciliation(
    postgres_catalog_requeue, monkeypatch,
):
    await _seed_dirty_bucket()
    repair_ready, resume_repair, apply_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_store = catalog_stats._store_tally
    repair_pid = apply_pid = 0
    tasks = []

    async def pause_old_repair(db, endpoint_id, day, tally, at, **kwargs):
        nonlocal repair_pid
        if asyncio.current_task() is tasks[0] and not repair_ready.is_set():
            repair_pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            repair_ready.set()
            await resume_repair.wait()
        await original_store(db, endpoint_id, day, tally, at, **kwargs)

    def before_apply_lock(conn, cursor, statement, parameters, context, executemany):
        nonlocal apply_pid
        normalized = " ".join(statement.lower().split())
        if (len(tasks) > 1 and asyncio.current_task() is tasks[1]
                and "from endpointstatcursor" in normalized and "for update" in normalized
                and not apply_attempted.is_set()):
            apply_pid = conn.connection.driver_connection.get_server_pid()
            apply_attempted.set()

    monkeypatch.setattr(catalog_stats, "_store_tally", pause_old_repair)
    event.listen(postgres_catalog_requeue.sync_engine, "before_cursor_execute", before_apply_lock)
    try:
        async with asyncio.timeout(15):
            tasks.append(asyncio.create_task(catalog_stats.refresh(
                session_maker, max_rows=0, now=EARLIER)))
            await repair_ready.wait()
            tasks.append(asyncio.create_task(catalog_stats.requeue_async_stats(
                session_maker, apply=True, now=NOW)))
            await apply_attempted.wait()
            await _wait_until_blocked(apply_pid, repair_pid)
            assert not tasks[1].done()
            resume_repair.set()
            repaired, manifest = await asyncio.gather(*tasks)
            assert repaired["repaired"] == 1 and manifest["applied"]
            assert manifest["targets"] == [{"endpoint_id": EP, "day": DAY}]

            # The old repair committed first. Apply must then invalidate its readiness and
            # recreate dirty work, rather than let that earlier success satisfy this request.
            async with session_maker() as db:
                bucket = await db.get(EndpointDayStat, (EP, DAY))
                marker = (await db.execute(select(EndpointStatDirty))).scalar_one()
                assert bucket.n == bucket.hits == 1 and not bucket.terminal_reconciled
                assert bucket.updated_at == EARLIER
                assert marker.revision == 1 and marker.queued_at == marker.retry_at == NOW
            status = await catalog_stats.repair_status(manifest, session_maker, now=NOW)
            assert not status["complete"] and status["pending"] == 1

            later = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
            assert later["repaired"] == 1
            status = await catalog_stats.repair_status(manifest, session_maker, now=NOW)
            assert status["complete"] and status["repaired"] == 1 and status["pending"] == 0
            async with session_maker() as db:
                bucket = await db.get(EndpointDayStat, (EP, DAY))
                assert bucket.n == bucket.hits == bucket.hit_decided == 1
                assert bucket.verdicts == {"valid": 1} and bucket.terminal_reconciled
    finally:
        resume_repair.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(postgres_catalog_requeue.sync_engine, "before_cursor_execute", before_apply_lock)
