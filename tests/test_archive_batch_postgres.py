"""Paid archive marks stay atomic with money without per-child database round trips."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, select, text

from treg import archive
from treg.application.call.settle import DeferredSettle, close_deferred
from treg.config import get_settings
from treg.domain import money
from treg.infra.db import reset_db, session_maker
from treg.infra.money_session import money_session
from treg.models import ArchiveKeyOrg, Org


@pytest.fixture
async def postgres_archive_batch(monkeypatch):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await reset_db()
    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    return engine


async def _batch(size: int):
    async with session_maker() as db:
        org = Org(name="paid-mark-batch", slug="paid-mark-batch")
        db.add(org)
        await db.flush()
        assert org.id is not None
        await money.grant(db, org.id, amount_micro=10_000)
        db.add_all([ArchiveKeyOrg(org_id=org.id, key_hash=f"key-{i:03}")
                    for i in range(size)])
        await db.commit()
        items = []
        for i in range(size):
            call = await money.reserve(db, org.id, "test.archive-batch", 100,
                                       call_id=f"archive-batch-{i}")
            items.append(DeferredSettle(
                call, True, 70, (org.id, f"key-{i:03}"), "", {}, payer_org_id=org.id))
        return org.id, items


async def _assert_paid(org_id: int, size: int):
    async with session_maker() as db:
        assert await money.balance_of(db, org_id) == 10_000 - size * 70
        assert sum(b.remaining_micro for b in await money.blocks_of(db, org_id)) == 10_000 - size * 70
        assert await money.open_holds_of(db, org_id) == []
        assert await money.spent_today(db, org_id) == size * 70
        assert await money.spent_today_from_ledger(db, org_id) == size * 70
        settled = [entry for entry in await money.entries_of(db, org_id)
                   if entry.kind == "settle"]
        assert len(settled) == size
        assert len({entry.call_id for entry in settled}) == size


async def test_deferred_archive_marks_use_bounded_database_round_trips(postgres_archive_batch):
    """The batch-size performance contract: marking children must not add one SELECT per child."""
    size = 13
    org_id, items = await _batch(size)
    archive_statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "ARCHIVEKEYORG" in statement.upper():
            archive_statements.append(statement)

    event.listen(postgres_archive_batch.sync_engine, "before_cursor_execute", capture)
    try:
        assert await close_deferred(items, charge=True) == size * 70
    finally:
        event.remove(postgres_archive_batch.sync_engine, "before_cursor_execute", capture)
    await _assert_paid(org_id, size)
    async with session_maker() as db:
        marks = (await db.execute(select(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == org_id))).scalars()
        assert {mark.key_hash: mark.calls for mark in marks} == {f"key-{i:03}": 2 for i in range(size)}
    assert len(archive_statements) <= 2, "paid marks should be batched, not add SQL per child"


async def test_paid_marks_take_no_sequence_number(postgres_archive_batch):
    """First and repeat marks are keyed by (org, question): a settle never waits on a sequence."""
    org_id, _ = await _batch(1)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "ARCHIVEKEYORG" in statement.upper():
            statements.append(statement.lower())

    event.listen(postgres_archive_batch.sync_engine, "before_cursor_execute", capture)
    try:
        async with session_maker() as db:
            await archive.note_org_uses_in_transaction(db, [(org_id, "key-000")] * 3
                                                       + [(org_id, "key-new")])
            await db.commit()
    finally:
        event.remove(postgres_archive_batch.sync_engine, "before_cursor_execute", capture)
    assert statements and not any("nextval" in s for s in statements)
    async with session_maker() as db:
        rows = (await db.execute(select(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == org_id))).scalars()
        assert {row.key_hash: (row.calls, row.id) for row in rows} == {
            "key-000": (4, None), "key-new": (1, None)}


async def test_opposite_org_and_key_orders_remain_compatible_across_chunks(postgres_archive_batch):
    from tests.test_money_lock_order import _contend_after_statement

    uses = [(org, f"key-{i:03}") for org in (2, 1) for i in range(101)]

    async def write(marks):
        async with session_maker() as db:
            await archive.note_org_uses_in_transaction(db, marks)
            await db.commit()

    assert await _contend_after_statement(
        postgres_archive_batch, lambda: write(uses), lambda: write(list(reversed(uses))),
        "INSERT INTO ARCHIVEKEYORG ",
    ) == [None, None]
    async with session_maker() as db:
        rows = (await db.execute(select(ArchiveKeyOrg))).scalars().all()
        assert len(rows) == 202
        assert all(row.calls == 2 for row in rows)


async def test_cancelling_after_first_archive_chunk_leaves_no_marks(postgres_archive_batch):
    reached = asyncio.Event()
    resume = asyncio.Event()

    async def pause(driver):
        reached.set()
        await resume.wait()

    def after_write(conn, cursor, statement, parameters, context, executemany):
        if not reached.is_set() and statement.lstrip().upper().startswith("INSERT INTO ARCHIVEKEYORG "):
            conn.connection.dbapi_connection.run_async(pause)

    async def write():
        async with money_session(session_maker()) as db:
            await archive.note_org_uses_in_transaction(db, [(1, f"key-{i:03}") for i in range(101)])
            await db.commit()

    event.listen(postgres_archive_batch.sync_engine, "after_cursor_execute", after_write)
    task = asyncio.create_task(write())
    try:
        async with asyncio.timeout(5):
            await reached.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(postgres_archive_batch.sync_engine, "after_cursor_execute", after_write)
    assert reached.is_set()
    assert postgres_archive_batch.pool.checkedout() == 0
    async with session_maker() as db:
        assert list((await db.execute(select(ArchiveKeyOrg))).scalars()) == []


async def test_deferred_keeps_org_and_archive_atomic_until_commit(postgres_archive_batch):
    """A competing reserve truly waits for the batch's Org until its archive work commits."""
    size = 13
    org_id, items = await _batch(size)
    archive_reached = asyncio.Event()
    resume = asyncio.Event()
    holder_pid = 0
    waiter_pid = asyncio.get_running_loop().create_future()
    tasks = []

    async def pause(driver):
        nonlocal holder_pid
        holder_pid = driver.get_server_pid()
        archive_reached.set()
        await resume.wait()

    def after_sql(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lstrip().upper()
        if (tasks and asyncio.current_task() is tasks[0] and not archive_reached.is_set()
                and "ARCHIVEKEYORG" in sql and sql.startswith(("UPDATE ", "INSERT "))):
            conn.connection.dbapi_connection.run_async(pause)

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if len(tasks) == 2 and asyncio.current_task() is tasks[1] and not waiter_pid.done():
            waiter_pid.set_result(conn.connection.driver_connection.get_server_pid())

    async def reserve():
        async with session_maker() as db:
            return await money.reserve(db, org_id, "test.archive-waiter", 100,
                                       call_id="competing-reserve")

    event.listen(postgres_archive_batch.sync_engine, "after_cursor_execute", after_sql)
    event.listen(postgres_archive_batch.sync_engine, "before_cursor_execute", before_sql)
    try:
        async with asyncio.timeout(10):
            tasks.append(asyncio.create_task(close_deferred(items, charge=True)))
            await archive_reached.wait()
            tasks.append(asyncio.create_task(reserve()))
            pid = await waiter_pid
            async with session_maker() as observer:
                while holder_pid not in (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                )).scalar_one():
                    await asyncio.sleep(.005)
            assert not tasks[1].done()
            resume.set()
            result = await asyncio.gather(*tasks)
            assert result == [size * 70, "competing-reserve"]
    finally:
        resume.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(postgres_archive_batch.sync_engine, "after_cursor_execute", after_sql)
        event.remove(postgres_archive_batch.sync_engine, "before_cursor_execute", before_sql)
    async with session_maker() as db:
        assert await money.release(db, "competing-reserve") == 100
    await _assert_paid(org_id, size)
