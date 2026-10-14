"""Match a PostgreSQL blocker to the active money transaction, without querying production."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra import money_trace
from treg.infra.db import reset_db, session_maker
from treg.models import Org


@pytest.fixture
async def postgres_trace(monkeypatch):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await reset_db()
    money_trace._reset_for_tests()
    money_trace.configure(enabled=True)
    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    monkeypatch.setattr(money_trace, "SLOW_TXN_SECONDS", 0)
    monkeypatch.setattr(money_trace, "SAMPLE_REPEAT_SECONDS", 0)
    yield engine
    money_trace._reset_for_tests()


async def _funded_holds():
    async with session_maker() as db:
        org = Org(name="trace-lock-holder", slug="trace-lock-holder")
        db.add(org)
        await db.flush()
        org_id = org.id
        assert org_id is not None
        await ledger.grant(db, org_id, amount_micro=1_000)
        await db.commit()
        holder = await ledger.reserve(db, org_id, "test.trace", 100, call_id="trace-holder")
        waiter = await ledger.reserve(db, org_id, "test.trace", 100, call_id="trace-waiter")
    money_trace.drain_events()
    return org_id, holder, waiter


async def _blocking_pids(waiter_pid: int, expected_blocker: int) -> list[int]:
    async with asyncio.timeout(5):
        async with session_maker() as observer:
            while True:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
                )).scalar_one()
                if expected_blocker in blockers:
                    return blockers
                await asyncio.sleep(0.01)


async def test_active_trace_identifies_the_real_block_holder_before_either_commit(
    postgres_trace, monkeypatch,
):
    org_id, holder_call, waiter_call = await _funded_holds()
    now = [0.0]
    monkeypatch.setattr(money_trace, "_clock", lambda: now[0])
    holder_paused = asyncio.Event()
    resume_holder = asyncio.Event()
    holder_pid = 0
    waiter_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    tasks = []

    async def pause_after_lock(driver_connection):
        nonlocal holder_pid
        holder_pid = driver_connection.get_server_pid()
        holder_paused.set()
        await resume_holder.wait()

    def after_sql(conn, cursor, statement, parameters, context, executemany):
        sql = statement.upper()
        if (tasks and asyncio.current_task() is tasks[0] and not holder_paused.is_set()
                and "FROM CREDITBLOCK" in sql and "FOR UPDATE" in sql):
            # PostgreSQL has granted the lock; pause the application before the next money step.
            conn.connection.dbapi_connection.run_async(pause_after_lock)

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if len(tasks) > 1 and asyncio.current_task() is tasks[1] and not waiter_pid.done():
            waiter_pid.set_result(conn.connection.driver_connection.get_server_pid())

    async def settle(call_id, amount):
        async with session_maker() as db:
            return await ledger.settle(db, call_id, amount, meta={
                "private_payload": "must-not-appear-in-await-chain@example.test",
            })

    event.listen(postgres_trace.sync_engine, "after_cursor_execute", after_sql)
    event.listen(postgres_trace.sync_engine, "before_cursor_execute", before_sql)
    try:
        async with asyncio.timeout(15):
            async with session_maker() as older_unrelated:
                money_trace.mark_money(older_unrelated, "release", org_id=909,
                                       call_id="older-unrelated-transaction")
                await older_unrelated.execute(text("SELECT 1"))
                now[0] = 10
                tasks.append(asyncio.create_task(settle(holder_call, 60)))
                await holder_paused.wait()
                now[0] = 20
                tasks.append(asyncio.create_task(settle(waiter_call, 70)))
                waiting_pid = await waiter_pid
                blockers = await _blocking_pids(waiting_pid, holder_pid)
                now[0] = 30
                money_trace.sample()
                events = money_trace.drain_events()
                active = [row for row in events if row["event"] == "money_txn_slow"]
                by_pid = {row["backend_pid"]: row for row in active}
                holder = by_pid[next(pid for pid in blockers if pid == holder_pid)]
                waiter = by_pid[waiting_pid]
                assert holder["org_ids"] == [org_id]
                assert holder["call_ids"] == [holder_call]
                assert holder["current_stage"] == "lock_blocks"
                assert holder["sql_inflight"] is False
                assert waiter["org_ids"] == [org_id]
                assert waiter["call_ids"] == [waiter_call]
                assert waiter["current_stage"] == "lock_blocks"
                assert waiter["sql_inflight"] is True
                assert waiter["await_chain"], "a real money coroutine must have source locations"
                for location in waiter["await_chain"]:
                    assert set(location) == {"file", "function", "line"}
                    assert location["file"].startswith("src/treg/")
                    assert isinstance(location["line"], int) and location["line"] > 0
                assert any(location["file"].endswith("domain/money/__init__.py")
                           for location in waiter["await_chain"])
                assert "must-not-appear-in-await-chain" not in repr(events)
                assert "private_payload" not in repr(events)
                assert holder["app_txn_id"] != waiter["app_txn_id"]
                assert holder["process_instance"] == waiter["process_instance"]
                assert max(active, key=lambda row: row["elapsed_ms"])["call_ids"] == [
                    "older-unrelated-transaction",
                ], "the longest transaction is deliberately not the lock holder"
                assert not [row for row in events if row["event"] == "money_txn_end"
                            and row["app_txn_id"] in {holder["app_txn_id"], waiter["app_txn_id"]}]
                resume_holder.set()
                assert await asyncio.gather(*tasks) == [60, 70]
                await older_unrelated.rollback()
        ended = [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
        assert {holder["app_txn_id"], waiter["app_txn_id"]} <= {
            row["app_txn_id"] for row in ended
        }
        money_trace.sample()
        assert not [row for row in money_trace.drain_events() if row["event"] == "money_txn_slow"]
    finally:
        resume_holder.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(postgres_trace.sync_engine, "after_cursor_execute", after_sql)
        event.remove(postgres_trace.sync_engine, "before_cursor_execute", before_sql)
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 870
        assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == 870
        assert await ledger.open_holds_of(db, org_id) == []
        assert await ledger.spent_today(db, org_id) == 130
        assert await ledger.spent_today_from_ledger(db, org_id) == 130


async def test_reused_backend_pid_has_a_new_transaction_id_and_no_stale_identity(postgres_trace):
    engine = create_async_engine(postgres_trace.url, pool_size=1, max_overflow=0)
    money_trace.install_engine(engine.sync_engine, pool_name="api")
    # Re-installation is harmless: it must not double-count each observed statement.
    money_trace.install_engine(engine.sync_engine, pool_name="api")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            money_trace.mark_money(db, "settle", org_id=7, call_id="first-pooled-call")
            first_pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            money_trace.sample()
            [first] = [row for row in money_trace.drain_events() if row["event"] == "money_txn_slow"]
            assert first["backend_pid"] == first_pid
            assert first["sql_count"] == 1
            await db.commit()
        money_trace.drain_events()
        async with sessions() as db:
            second_pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert second_pid == first_pid, "the single-connection pool really reused this backend"
            money_trace.sample()
            assert not [row for row in money_trace.drain_events() if row["event"] == "money_txn_slow"]
            money_trace.mark_money(db, "release", org_id=8, call_id="second-pooled-call")
            money_trace.sample()
            [second] = [row for row in money_trace.drain_events() if row["event"] == "money_txn_slow"]
            assert second["backend_pid"] == first["backend_pid"]
            assert second["app_txn_id"] != first["app_txn_id"]
            assert second["org_ids"] == [8]
            assert second["call_ids"] == ["second-pooled-call"]
            assert second["sql_count"] == 1
            await db.rollback()
    finally:
        await engine.dispose()
    money_trace.sample()
    assert not [row for row in money_trace.drain_events() if row["event"] == "money_txn_slow"]


async def test_cancelling_a_real_lock_wait_clears_its_trace_and_preserves_the_hold(postgres_trace):
    org_id, holder_call, waiter_call = await _funded_holds()
    waiter_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = None

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if asyncio.current_task() is task and not waiter_pid.done():
            waiter_pid.set_result(conn.connection.driver_connection.get_server_pid())

    async def waiting_settle():
        async with session_maker() as db:
            return await ledger.settle(db, waiter_call, 70)

    event.listen(postgres_trace.sync_engine, "before_cursor_execute", before_sql)
    try:
        async with asyncio.timeout(15):
            async with session_maker() as holder_db:
                assert await ledger.settle_in_transaction(holder_db, holder_call, 60) == 60
                money_trace.sample()
                [holder] = [row for row in money_trace.drain_events()
                            if row["event"] == "money_txn_slow"]
                task = asyncio.create_task(waiting_settle())
                pid = await waiter_pid
                await _blocking_pids(pid, holder["backend_pid"])
                money_trace.sample()
                [waiting] = [row for row in money_trace.drain_events()
                             if row["event"] == "money_txn_slow" and row["backend_pid"] == pid]
                assert waiting["sql_inflight"] and waiting["current_stage"] == "lock_blocks"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                ended = [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"
                         and row["app_txn_id"] == waiting["app_txn_id"]]
                assert len(ended) == 1 and ended[0]["outcome"] == "cancelled"
                money_trace.sample()
                assert not [row for row in money_trace.drain_events() if row["event"] == "money_txn_slow"
                            and row["app_txn_id"] == waiting["app_txn_id"]]
                await holder_db.commit()
    finally:
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        event.remove(postgres_trace.sync_engine, "before_cursor_execute", before_sql)
    async with session_maker() as db:
        assert [hold.id for hold in await ledger.open_holds_of(db, org_id)] == [waiter_call]
        assert await ledger.balance_of(db, org_id) == 840
        assert await ledger.settle(db, waiter_call, 70) == 70
        assert await ledger.balance_of(db, org_id) == 870
        assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == 870
        assert await ledger.open_holds_of(db, org_id) == []
        closes = [entry for entry in await ledger.entries_of(db, org_id) if entry.kind == "settle"]
        assert sorted((entry.call_id, entry.amount_micro) for entry in closes) == [
            (holder_call, -60), (waiter_call, -70),
        ]
