"""Catalog recount races and cancellation against an explicitly configured test PostgreSQL.

SQLite cannot exercise row-lock ordering or driver cancellation. These tests use the suite's
isolated TREG_TEST_DB_URL only, and never open a separate database or production connection.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select, text

from treg.application import catalog_stats
from treg.domain.catalog import stats
from treg.domain.catalog import store as catalog_store
from treg.infra import catalog_stat_dirty
from treg.infra.catalog_observations import (
    EndpointObservationUnavailable, PostgresEndpointObservationReader, STALE_AFTER_S,
)
from treg.infra.catalog_stat_dirty import mark_dirty
from treg.infra.db import reset_db, session_maker
from treg.models import CallRecord, EndpointDayStat, EndpointStatCursor, EndpointStatDirty
from treg import timeutil

NOW = datetime(2026, 10, 15, 12)
SUBMITTED = datetime(2026, 10, 14, 23)
DAY = "2026-10-14"
EP = "wiza.people.email.find"


@pytest.fixture
async def postgres_catalog_stats(monkeypatch):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await reset_db()
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: False)
    return engine


async def _record(endpoint: str = EP, *, hit: bool | None = None) -> int:
    async with session_maker() as db:
        row = CallRecord(user_email="test@example.test", tool_name=endpoint, method="GET",
                         path="/x", endpoint_id=endpoint, status_code=200, duration_ms=100,
                         hit=hit, cost_observed_micro=0, created_at=SUBMITTED)
        db.add(row)
        await db.commit()
        assert row.id is not None
        return row.id


async def _correct(row_id: int, hit: bool, *, now: datetime = NOW) -> None:
    async with session_maker() as db:
        row = await db.get(CallRecord, row_id)
        row.hit = hit
        row.verdict = "valid" if hit else "not_found"
        db.add(row)
        await mark_dirty(db, {(row.endpoint_id, DAY)}, now=now)
        await db.commit()


async def _bucket(endpoint: str = EP) -> EndpointDayStat:
    async with session_maker() as db:
        return await db.get(EndpointDayStat, (endpoint, DAY))


async def _markers() -> list[EndpointStatDirty]:
    async with session_maker() as db:
        return list((await db.execute(select(EndpointStatDirty).order_by(
            EndpointStatDirty.endpoint_id))).scalars())


async def _cleanup_tasks(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_until_blocked(waiter_pid: int, blocker_pid: int) -> None:
    async with asyncio.timeout(5):
        async with session_maker() as observer:
            while True:
                blockers = (await observer.execute(text("SELECT pg_blocking_pids(:pid)"),
                                                   {"pid": waiter_pid})).scalar_one()
                if blocker_pid in blockers:
                    return
                await asyncio.sleep(0.01)


async def test_terminal_correction_commits_during_recount_and_keeps_new_revision(
    postgres_catalog_stats, monkeypatch,
):
    clock = [NOW]
    monkeypatch.setattr(catalog_stats, "utcnow_naive", lambda: clock[0])
    monkeypatch.setattr(timeutil, "utcnow_naive", lambda: clock[0])
    monkeypatch.setattr(stats, "_now", lambda: clock[0])
    row_id = await _record()
    await catalog_stats.refresh(session_maker, now=NOW)
    originally_queued = NOW - timedelta(hours=3)
    await _correct(row_id, True, now=originally_queued)
    reader = PostgresEndpointObservationReader(session_maker)
    with pytest.raises(EndpointObservationUnavailable):
        await reader.get_many([EP])
    read_done, resume = asyncio.Event(), asyncio.Event()
    original_store = catalog_stats._store_tally
    paused = False

    async def pause_before_store(db, endpoint_id, day, tally, at, **kwargs):
        nonlocal paused
        if not paused and kwargs["terminal_reconciled"]:
            paused = True
            read_done.set()
            await resume.wait()
        await original_store(db, endpoint_id, day, tally, at, **kwargs)

    monkeypatch.setattr(catalog_stats, "_store_tally", pause_before_store)
    # A long-running cron pass can have an older `now` than this bucket's source snapshot.
    # The successful recount covers corrections up to its scan start, not its later commit.
    scan_started_at = NOW + timedelta(minutes=5)
    clock[0] = scan_started_at
    repair = asyncio.create_task(catalog_stats.refresh(session_maker, max_rows=0, now=NOW))
    try:
        async with asyncio.timeout(15):
            await read_done.wait()
            # This is a different real transaction, committed while repair holds the cursor
            # lock. Source audit rows and marker revisions must remain writable throughout.
            clock[0] = scan_started_at + timedelta(minutes=7)
            await _correct(row_id, False, now=clock[0])
            marker, = await _markers()
            assert marker.revision == 2
            assert marker.queued_at == marker.retry_at == originally_queued
            clock[0] = scan_started_at + timedelta(minutes=10)
            resume.set()
            result = await repair
            assert result["repaired"] == result["repair_pending"] == 1
            assert (await _bucket()).hits == 1  # the source snapshot preceded the newer commit
            marker, = await _markers()
            assert marker.revision == 2
            assert marker.queued_at == scan_started_at
            assert marker.queued_at < clock[0], "commit time must not hide post-snapshot corrections"
            assert marker.retry_at == originally_queued
            assert (await reader.get_many([EP]))[EP]["hit_samples"] == 1

            # Keeping the append cursor healthy cannot indefinitely hide a stranded correction.
            clock[0] = scan_started_at + timedelta(seconds=STALE_AFTER_S + 1)
            append_only = await catalog_stats.refresh(session_maker, max_dirty_buckets=0, now=clock[0])
            assert append_only["caught_up"] and append_only["repaired"] == 0
            assert append_only["repair_pending"] == 1
            assert (await _markers())[0].queued_at == scan_started_at
            with pytest.raises(EndpointObservationUnavailable) as unavailable:
                await reader.get_many([EP])
            assert unavailable.value.completed == {}

            later = await catalog_stats.refresh(session_maker, max_rows=0, now=clock[0])
            assert later["repaired"] == 1 and later["repair_pending"] == 0
            bucket = await _bucket()
            assert bucket.n == bucket.hit_decided == 1 and bucket.hits == 0
            assert bucket.verdicts == {"not_found": 1}
            assert await _markers() == []
    finally:
        resume.set()
        await _cleanup_tasks([repair])


async def test_append_fold_waits_for_repair_cursor_lock_without_double_counting(
    postgres_catalog_stats, monkeypatch,
):
    first_id = await _record()
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(first_id, True)
    second_id = await _record(hit=True)  # not yet included in the persisted fold cursor
    repair_read, resume_repair, append_attempt = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_store = catalog_stats._store_tally
    repair_pid = 0
    append_pid = 0
    tasks = []

    async def pause_repair(db, endpoint_id, day, tally, at, **kwargs):
        nonlocal repair_pid
        if asyncio.current_task() is tasks[0] and not repair_read.is_set():
            repair_pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            repair_read.set()
            await resume_repair.wait()
        await original_store(db, endpoint_id, day, tally, at, **kwargs)

    def before_append_lock(conn, cursor, statement, parameters, context, executemany):
        nonlocal append_pid
        normalized = " ".join(statement.lower().split())
        if (len(tasks) > 1 and asyncio.current_task() is tasks[1]
                and "from endpointstatcursor" in normalized and "for update" in normalized
                and not append_attempt.is_set()):
            append_pid = conn.connection.driver_connection.get_server_pid()
            append_attempt.set()

    monkeypatch.setattr(catalog_stats, "_store_tally", pause_repair)
    event.listen(postgres_catalog_stats.sync_engine, "before_cursor_execute", before_append_lock)
    try:
        async with asyncio.timeout(15):
            tasks.append(asyncio.create_task(catalog_stats.refresh(session_maker, max_rows=0, now=NOW)))
            await repair_read.wait()
            tasks.append(asyncio.create_task(catalog_stats.refresh(
                session_maker, max_dirty_buckets=0, now=NOW)))
            await append_attempt.wait()
            await _wait_until_blocked(append_pid, repair_pid)
            assert not tasks[1].done()
            resume_repair.set()
            repaired, appended = await asyncio.gather(*tasks)
            assert repaired["repaired"] == 1 and appended["rows"] == 1
            bucket = await _bucket()
            assert bucket.n == bucket.ok == bucket.hits == bucket.hit_decided == 2
            async with session_maker() as db:
                cursor = await db.get(EndpointStatCursor, catalog_stats.CURSOR_ID)
                assert cursor.cursor_id == second_id
            assert await _markers() == []
    finally:
        resume_repair.set()
        await _cleanup_tasks(tasks)
        event.remove(postgres_catalog_stats.sync_engine, "before_cursor_execute", before_append_lock)


async def test_correction_waiting_on_acknowledgement_delete_recreates_a_new_marker(
    postgres_catalog_stats, monkeypatch,
):
    row_id = await _record()
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(row_id, True)
    scan_started_at = NOW + timedelta(minutes=1)
    newly_queued_at = scan_started_at + timedelta(minutes=1)
    monkeypatch.setattr(catalog_stats, "utcnow_naive", lambda: scan_started_at)
    deleted, resume, audit_attempt = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_acknowledge = catalog_stat_dirty.acknowledge
    repair_pid = audit_pid = 0
    tasks = []

    async def pause_after_matching_delete(db, *args, **kwargs):
        original_execute = db.execute

        async def execute(statement, *execute_args, **execute_kwargs):
            nonlocal repair_pid
            result = await original_execute(statement, *execute_args, **execute_kwargs)
            if getattr(statement, "is_delete", False) and not deleted.is_set():
                assert result.rowcount == 1, "the real matching-revision DELETE must own the row lock"
                repair_pid = (await original_execute(text("SELECT pg_backend_pid()"))).scalar_one()
                deleted.set()
                await resume.wait()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", execute)
            await original_acknowledge(db, *args, **kwargs)

    def before_audit_upsert(conn, cursor, statement, parameters, context, executemany):
        nonlocal audit_pid
        if (len(tasks) > 1 and asyncio.current_task() is tasks[1]
                and "insert into endpointstatdirty" in " ".join(statement.lower().split())):
            audit_pid = conn.connection.driver_connection.get_server_pid()
            audit_attempt.set()

    monkeypatch.setattr(catalog_stat_dirty, "acknowledge", pause_after_matching_delete)
    event.listen(postgres_catalog_stats.sync_engine, "before_cursor_execute", before_audit_upsert)
    try:
        async with asyncio.timeout(15):
            tasks.append(asyncio.create_task(catalog_stats.refresh(session_maker, max_rows=0, now=NOW)))
            await deleted.wait()
            tasks.append(asyncio.create_task(_correct(row_id, False, now=newly_queued_at)))
            await audit_attempt.wait()
            await _wait_until_blocked(audit_pid, repair_pid)
            assert not tasks[1].done()
            resume.set()
            repaired, _ = await asyncio.gather(*tasks)
            assert repaired["repaired"] == 1
            assert (await _bucket()).hits == 1  # the newer correction was outside the source snapshot
            marker, = await _markers()
            assert marker.revision == 1, "the post-DELETE upsert must recreate rather than lose its marker"
            assert marker.queued_at == marker.retry_at == newly_queued_at
            async with session_maker() as db:
                assert (await db.get(CallRecord, row_id)).hit is False
    finally:
        resume.set()
        await _cleanup_tasks(tasks)
        event.remove(postgres_catalog_stats.sync_engine, "before_cursor_execute", before_audit_upsert)


async def test_acknowledgement_does_not_move_a_newer_markers_age_backwards(postgres_catalog_stats):
    queued_at, retry_at = NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)
    async with session_maker() as db:
        db.add(EndpointStatDirty(endpoint_id=EP, day=DAY, revision=2,
                                 queued_at=queued_at, retry_at=retry_at))
        await db.commit()
    async with session_maker() as db:
        await catalog_stat_dirty.acknowledge(db, EP, DAY, 1, scanned_at=NOW)
        await db.commit()
    marker, = await _markers()
    assert marker.revision == 2
    assert marker.queued_at == queued_at and marker.retry_at == retry_at


async def test_postgres_query_cancellation_rolls_back_replacement_and_reuses_pool(
    postgres_catalog_stats, monkeypatch,
):
    hot, cool = "test.repair.a_hot", "test.repair.b_cool"
    hot_id, cool_id = [await _record(endpoint) for endpoint in (hot, cool)]
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(hot_id, True)
    await _correct(cool_id, True)
    sleep_started = asyncio.Event()
    original_store = catalog_stats._store_tally

    async def stall_after_write(db, endpoint_id, day, tally, at, **kwargs):
        await original_store(db, endpoint_id, day, tally, at, **kwargs)
        if endpoint_id == hot and not sleep_started.is_set():
            sleep_started.set()
            # Cancel real PostgreSQL I/O after a write, so both transaction rollback and the
            # asyncpg connection cancellation path are exercised, rather than a Python stub.
            await db.execute(text("SELECT pg_sleep(5)"))

    monkeypatch.setattr(catalog_stats, "_store_tally", stall_after_write)
    async with asyncio.timeout(15):
        result = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW,
                                             bucket_timeout_s=0.2, max_repair_seconds=5)
        assert sleep_started.is_set()
        assert result["repaired"] == result["repair_failed"] == result["repair_pending"] == 1
        assert (await _bucket(hot)).hits == 0  # the already-executed replacement rolled back
        assert (await _bucket(cool)).hits == 1
        marker, = await _markers()
        assert marker.endpoint_id == hot and marker.revision == 1 and marker.retry_at > NOW
        assert marker.queued_at == NOW, "failed replacements must not renew correction age"

        # A cancelled asyncpg connection may be discarded, but the pool and cursor lock must be
        # immediately usable by another session. NOWAIT proves no abandoned lock survives.
        async with session_maker() as db:
            assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
            cursor = (await db.execute(select(EndpointStatCursor).where(
                EndpointStatCursor.id == catalog_stats.CURSOR_ID).with_for_update(nowait=True)
            )).scalar_one()
            assert cursor.cursor_id == cool_id
            await db.rollback()
        later = await catalog_stats.refresh(session_maker, max_rows=0,
                                             now=marker.retry_at + timedelta(seconds=1))
        assert later["repaired"] == 1 and later["repair_pending"] == 0
        assert (await _bucket(hot)).n == (await _bucket(hot)).hits == 1
