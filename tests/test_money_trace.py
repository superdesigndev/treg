"""Transaction tracing is diagnostic: it must not change money or database ownership."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, text

from treg.config import get_settings
from treg.domain import money as ledger

from treg.infra import money_trace
from treg.infra.db import reset_db, session_maker
from treg.models import Org


@pytest.fixture
async def trace_db(monkeypatch):
    await reset_db()
    money_trace._reset_for_tests()
    money_trace.configure(enabled=True)
    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    monkeypatch.setattr(money_trace, "SLOW_TXN_SECONDS", 0)
    monkeypatch.setattr(money_trace, "SAMPLE_REPEAT_SECONDS", 0)
    yield session_maker.kw["bind"]
    money_trace._reset_for_tests()


async def test_marking_an_unused_session_never_checks_out_a_connection_or_runs_sql(trace_db):
    checkouts = []
    statements = []

    def checkout(connection, record, proxy):
        checkouts.append(connection)

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(trace_db.sync_engine, "checkout", checkout)
    event.listen(trace_db.sync_engine, "before_cursor_execute", before_sql)
    try:
        async with session_maker() as db:
            money_trace.mark_money(db, "settle", org_id=7, call_id="not-yet-a-transaction")
            with money_trace.money_stage(db, "lock_blocks"):
                money_trace.sample()
            assert money_trace.drain_events() == []
        assert checkouts == []
        assert statements == []
    finally:
        event.remove(trace_db.sync_engine, "checkout", checkout)
        event.remove(trace_db.sync_engine, "before_cursor_execute", before_sql)


async def _funded_org():
    async with session_maker() as db:
        org = Org(name="transaction-trace", slug="transaction-trace")
        db.add(org)
        await db.flush()
        org_id = org.id
        assert org_id is not None
        await ledger.grant(db, org_id, amount_micro=1_000)
        await db.commit()
    money_trace.drain_events()
    return org_id


def _active_events():
    money_trace.sample()
    return [row for row in money_trace.drain_events() if row["event"] == "money_txn_slow"]


async def test_tracing_preserves_money_results_and_the_exact_sql_sequence(trace_db):
    org_id = await _funded_org()
    statements = []

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(trace_db.sync_engine, "before_cursor_execute", before_sql)
    try:
        results = []
        for enabled, call_id in ((False, "without-trace"), (True, "with-trace")):
            money_trace.configure(enabled=enabled)
            statements.clear()
            async with session_maker() as db:
                hold = await ledger.reserve(db, org_id, "test.trace", 100, call_id=call_id)
                assert await ledger.settle(db, hold, 60) == 60
            results.append(list(statements))
        assert results[0] == results[1], "diagnostics cannot add even a PID/transaction-id query"
    finally:
        event.remove(trace_db.sync_engine, "before_cursor_execute", before_sql)
        money_trace.configure(enabled=True)
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 880
        assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == 880
        assert await ledger.open_holds_of(db, org_id) == []
        assert await ledger.spent_today(db, org_id) == 120


@pytest.mark.parametrize("ending,outcome", [
    ("commit", "committed"), ("rollback", "rolled_back"),
    ("close", "rolled_back"), ("invalidate", "invalidated"),
])
async def test_ended_session_transaction_cannot_tag_its_next_transaction(trace_db, ending, outcome):
    async with session_maker() as db:
        money_trace.mark_money(db, "settle", org_id=7, call_id="previous-call")
        await db.execute(text("SELECT 1"))
        [active] = _active_events()
        assert active["org_ids"] == [7] and active["call_ids"] == ["previous-call"]
        await getattr(db, ending)()
        ended = [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
        assert len(ended) == 1
        assert ended[0]["app_txn_id"] == active["app_txn_id"]
        assert ended[0]["outcome"] == outcome
        assert _active_events() == []
        # Reusing the same Python session for an unrelated read must not resurrect old identity.
        await db.execute(text("SELECT 2"))
        assert _active_events() == []
        money_trace.mark_money(db, "release", org_id=8, call_id="next-call")
        [next_active] = _active_events()
        assert next_active["app_txn_id"] != active["app_txn_id"]
        assert next_active["org_ids"] == [8]
        assert next_active["call_ids"] == ["next-call"]
        await db.rollback()


async def test_cancellation_propagates_and_clears_the_active_transaction(trace_db):
    reached = asyncio.Event()
    never = asyncio.Event()

    async def cancelled_work():
        async with session_maker() as db:
            money_trace.mark_money(db, "settle", org_id=7, call_id="cancelled-call")
            with money_trace.money_stage(db, "lock_blocks"):
                await db.execute(text("SELECT 1"))
                reached.set()
                await never.wait()

    task = asyncio.create_task(cancelled_work())
    try:
        async with asyncio.timeout(5):
            await reached.wait()
            [active] = _active_events()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        ended = [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
        assert len(ended) == 1
        assert ended[0]["app_txn_id"] == active["app_txn_id"]
        assert ended[0]["outcome"] == "cancelled"
        assert _active_events() == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_topup_flush_and_savepoint_do_not_end_the_owning_transaction(trace_db):
    org_id = await _funded_org()
    async with session_maker() as db:
        await ledger.topup(db, org_id, 250, "trace-payment")
        await db.flush()
        events = money_trace.drain_events()
        assert not [row for row in events if row["event"] == "money_txn_end"]
        [active] = _active_events()
        assert active["operation"] == "topup"
        assert active["org_ids"] == [org_id]
        assert active["txn_ended_at"] is None
        await db.commit()
        ended = [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
        assert len(ended) == 1
        assert ended[0]["app_txn_id"] == active["app_txn_id"]
        assert ended[0]["outcome"] == "committed"
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 1_250


async def test_diagnostic_clock_failure_does_not_change_commit_or_leave_a_false_blocker(
    trace_db, monkeypatch,
):
    org_id = await _funded_org()
    async with session_maker() as db:
        hold = await ledger.reserve(db, org_id, "test.trace", 100, call_id="clock-failure")
        money_trace.drain_events()
        assert await ledger.settle_in_transaction(db, hold, 60) == 60
        [active] = _active_events()
        with monkeypatch.context() as broken:
            def unavailable():
                raise RuntimeError("diagnostic clock unavailable")
            broken.setattr(money_trace, "_clock", unavailable)
            await db.commit()
        assert not [row for row in _active_events() if row["app_txn_id"] == active["app_txn_id"]]
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 940
        assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == 940
        assert await ledger.open_holds_of(db, org_id) == []
        assert await ledger.spent_today(db, org_id) == 60


async def test_full_event_queue_drops_diagnostics_without_changing_money(trace_db, monkeypatch):
    org_id = await _funded_org()
    monkeypatch.setattr(money_trace, "MAX_QUEUED", 2)
    for index in range(6):
        async with session_maker() as db:
            hold = await ledger.reserve(db, org_id, "test.trace", 100, call_id=f"queue-full-{index}")
            assert await ledger.settle(db, hold, 10) == 10
    events = money_trace.drain_events()
    assert len([row for row in events if row["event"] != "money_trace_dropped"]) <= 2
    dropped = [row for row in events if row["event"] == "money_trace_dropped"]
    assert len(dropped) == 1 and dropped[0]["dropped_events"] > 0
    assert money_trace.drain_events() == [], "draining twice must not count the same drop twice"
    assert _active_events() == []
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 940
        assert await ledger.open_holds_of(db, org_id) == []
        assert await ledger.spent_today(db, org_id) == 60


async def test_full_active_registry_retains_an_existing_possible_blocker(trace_db, monkeypatch):
    monkeypatch.setattr(money_trace, "MAX_ACTIVE", 1)
    async with session_maker() as first, session_maker() as second:
        money_trace.mark_money(first, "settle", org_id=1, call_id="first-possible-blocker")
        await first.execute(text("SELECT 1"))
        money_trace.mark_money(second, "settle", org_id=2, call_id="newer-waiter")
        await second.execute(text("SELECT 2"))
        money_trace.sample()
        events = money_trace.drain_events()
        active = [row for row in events if row["event"] == "money_txn_slow"]
        assert len(active) == 1 and active[0]["call_ids"] == ["first-possible-blocker"]
        assert any(row["event"] == "money_trace_dropped" and row["dropped_active"] > 0
                   for row in events)
        await second.rollback()
        await first.rollback()
    assert _active_events() == []


async def test_samples_distinguish_sql_time_from_application_gap_without_retaining_sql(
    trace_db, monkeypatch,
):
    now = [0.0]
    monkeypatch.setattr(money_trace, "_clock", lambda: now[0])
    private_value = "do-not-log-sql-parameter@example.test"

    def database_time(conn, cursor, statement, parameters, context, executemany):
        now[0] += 2

    event.listen(trace_db.sync_engine, "before_cursor_execute", database_time)
    try:
        async with session_maker() as db:
            money_trace.mark_money(db, "settle", org_id=7, call_id="bounded-call")
            with money_trace.money_stage(db, "lock_blocks"):
                await db.execute(text("SELECT :private_value"), {"private_value": private_value})
                now[0] += 3
                [active] = _active_events()
                assert active["current_stage"] == "lock_blocks"
                assert active["sql_inflight"] is False
                assert active["sql_count"] == 1
                assert active["sql_total_ms"] == 2_000
                assert active["gap_since_sql_ms"] == 3_000
                assert active["elapsed_ms"] == 5_000
                assert private_value not in repr(active)
                assert "SELECT" not in repr(active) and "private_value" not in repr(active)
            await db.rollback()
    finally:
        event.remove(trace_db.sync_engine, "before_cursor_execute", database_time)


async def test_identity_and_stage_history_are_bounded_and_unsafe_labels_are_not_logged(trace_db):
    async with session_maker() as db:
        money_trace.mark_money(db, "settle", org_id=1, call_id="first-call")
        await db.execute(text("SELECT 1"))
        for index in range(50):
            money_trace.mark_money(db, org_id=index + 2, call_id=f"batch-call-{index}")
            with money_trace.money_stage(db, f"stage_{index}"):
                pass
        unsafe = "customer@example.test\nrequest-body"
        money_trace.mark_money(db, unsafe, org_id=True, call_id=unsafe)
        with money_trace.money_stage(db, unsafe):
            [active] = _active_events()
        assert len(active["org_ids"]) <= money_trace.MAX_IDENTITIES
        assert len(active["call_ids"]) <= money_trace.MAX_IDENTITIES
        assert active["identities_truncated"] is True
        assert active["org_ids"][0] == 1 and active["call_ids"][0] == "first-call"
        assert len(active["stage_history"]) <= money_trace.MAX_STAGES
        assert active["stage_history_dropped"] > 0
        assert len(active["stage_totals_ms"]) <= money_trace.MAX_STAGES + 1
        assert unsafe not in repr(active) and "customer@example.test" not in repr(active)
        await db.rollback()


async def test_shutdown_drains_active_transactions_without_committing_them(trace_db):
    async with session_maker() as db:
        money_trace.mark_money(db, "settle", org_id=7, call_id="shutdown-active")
        await db.execute(text("SELECT 1"))
        events = money_trace.shutdown_events()
        active = [row for row in events if row["event"] == "money_txn_slow"]
        assert len(active) == 1 and active[0]["call_ids"] == ["shutdown-active"]
        assert not [row for row in events if row["event"] == "money_txn_end"]
        assert db.in_transaction()
        await db.rollback()


async def test_nested_stages_restore_the_parent_and_commit_is_visible_before_it_finishes(trace_db):
    at_commit = []

    def committing(connection):
        money_trace.sample()
        at_commit.extend(money_trace.drain_events())

    event.listen(trace_db.sync_engine, "commit", committing)
    try:
        async with session_maker() as db:
            money_trace.mark_money(db, "settle", org_id=7, call_id="nested-stage-call")
            with money_trace.money_stage(db, "claim_hold"):
                await db.execute(text("SELECT 1"))
                with money_trace.money_stage(db, "lock_blocks"):
                    [inner] = _active_events()
                    assert inner["current_stage"] == "lock_blocks"
                [parent] = _active_events()
                assert parent["current_stage"] == "claim_hold"
                assert parent["app_txn_id"] == inner["app_txn_id"]
            [between] = _active_events()
            assert between["current_stage"] == "between_stages"
            await db.commit()
        [committing_row] = [row for row in at_commit if row["event"] == "money_txn_slow"]
        assert committing_row["app_txn_id"] == inner["app_txn_id"]
        assert committing_row["current_stage"] == "commit"
        assert committing_row["txn_ended_at"] is None
        [ended] = [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
        assert ended["outcome"] == "committed"
        assert ended["app_txn_id"] == inner["app_txn_id"]
    finally:
        event.remove(trace_db.sync_engine, "commit", committing)


async def test_diagnostic_failure_cannot_replace_a_business_error_or_commit_its_hold(
    trace_db, monkeypatch,
):
    org_id = await _funded_org()
    business_error = ValueError("the caller must receive this exact failure")
    with pytest.raises(ValueError) as raised:
        async with session_maker() as db:
            await ledger.reserve_in_transaction(db, org_id, "test.trace", 100,
                                                call_id="rolled-back-business-failure")
            with monkeypatch.context() as broken:
                def unavailable():
                    raise RuntimeError("diagnostic clock unavailable")
                broken.setattr(money_trace, "_clock", unavailable)
                with money_trace.money_stage(db, "lock_blocks"):
                    raise business_error
    assert raised.value is business_error
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 1_000
        assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == 1_000
        assert await ledger.open_holds_of(db, org_id) == []
        assert await ledger.spent_today(db, org_id) == 0
    assert _active_events() == []


async def test_invalidation_and_late_diagnostics_do_not_reconnect_the_session(trace_db):
    checkouts = []
    statements = []

    def checkout(connection, record, proxy):
        checkouts.append(connection)

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(trace_db.sync_engine, "checkout", checkout)
    event.listen(trace_db.sync_engine, "before_cursor_execute", before_sql)
    try:
        async with session_maker() as db:
            money_trace.mark_money(db, "settle", org_id=7, call_id="invalidated-trace")
            await db.execute(text("SELECT 1"))
            assert len(checkouts) == len(statements) == 1
            await db.invalidate()
            money_trace.sample()
            money_trace.drain_events()
            money_trace.shutdown_events()
        assert len(checkouts) == len(statements) == 1, "diagnostics cannot revive an invalid connection"
    finally:
        event.remove(trace_db.sync_engine, "checkout", checkout)
        event.remove(trace_db.sync_engine, "before_cursor_execute", before_sql)


async def test_failed_savepoint_flush_restores_parent_stage_without_ending_the_transaction(trace_db):
    from sqlalchemy.exc import IntegrityError

    org_id = await _funded_org()
    async with session_maker() as db:
        money_trace.mark_money(db, "topup", org_id=org_id, call_id="savepoint-unique-race")
        with money_trace.money_stage(db, "lookup_payment"):
            await db.execute(text("SELECT 1"))
            [before] = _active_events()
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    db.add(Org(name="duplicate-slug", slug="transaction-trace"))
                    await db.flush()
            assert not [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
            await db.execute(text("SELECT 2"))
            [after] = _active_events()
            assert after["app_txn_id"] == before["app_txn_id"]
            assert after["current_stage"] == "lookup_payment"
            assert after["outcome"] == "open"
        await db.commit()
        [ended] = [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
        assert ended["app_txn_id"] == before["app_txn_id"]
        assert ended["outcome"] == "committed"


async def test_root_failed_flush_not_active_after_server_rollback(trace_db, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    org_id = await _funded_org()
    now = [0.0]
    monkeypatch.setattr(money_trace, "_clock", lambda: now[0])
    async with session_maker() as db:
        money_trace.mark_money(db, "topup", org_id=org_id, call_id="failed-root-flush")
        await db.execute(text("SELECT 1"))
        [before] = _active_events()
        now[0] = 2.0
        with pytest.raises(IntegrityError):
            db.add(Org(name="duplicate-root-slug", slug="transaction-trace"))
            await db.flush()
        # SQLAlchemy still requires an explicit session rollback, but PostgreSQL/SQLite has
        # already rolled the transaction back. It must not appear as a live lock holder now.
        assert db.in_transaction() and not db.is_active
        money_trace.sample()
        events = money_trace.drain_events()
        assert not [row for row in events if row["event"] == "money_txn_slow"]
        [ended] = [row for row in events if row["event"] == "money_txn_end"]
        assert ended["app_txn_id"] == before["app_txn_id"]
        assert ended["outcome"] == "rolled_back"
        assert ended["elapsed_ms"] == 2000, "session cleanup time is not database transaction time"
        now[0] = 12.0
        with money_trace.money_stage(db, "application_cleanup"):
            assert _active_events() == []
        await db.rollback()
        assert not [row for row in money_trace.drain_events() if row["event"] == "money_txn_end"]
        assert "application_cleanup" not in ended["stage_totals_ms"]
        await db.execute(text("SELECT 2"))
        assert _active_events() == []
        money_trace.mark_money(db, "grant", org_id=org_id, call_id="after-root-rollback")
        [next_transaction] = _active_events()
        assert next_transaction["app_txn_id"] != before["app_txn_id"]
        assert next_transaction["call_ids"] == ["after-root-rollback"]
        await db.commit()
