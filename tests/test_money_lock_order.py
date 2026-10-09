"""Money's concurrent transaction contract, exercised against real PostgreSQL locks.

Run with TREG_TEST_DB_URL pointing at an isolated test database. SQLite cannot exercise these
waits. SQLAlchemy events pause SQL at the database boundary; no money operation is mocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, text

from treg.application.call.settle import DeferredSettle, close_deferred
from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import reset_db, session_maker
from treg.models import AdConversion, Hold, Org


@pytest.fixture
async def postgres_money(monkeypatch):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await reset_db()
    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    return engine


async def _funded_org(slug: str) -> int:
    async with session_maker() as db:
        org = Org(name=slug, slug=slug)
        db.add(org)
        await db.flush()
        org_id = org.id
        assert org_id is not None
        await ledger.grant(db, org_id, amount_micro=1_000)
        await db.commit()
        return org_id


async def _reserve(org_id: int, call_id: str) -> str:
    async with session_maker() as db:
        return await ledger.reserve(
            db, org_id, "test.money", 100, call_id=call_id,
            tags={"customer": call_id},
        )


async def _wait_for_blocker(waiter_pid: int, blocker_pid: int) -> None:
    """Observe an actual server-side wait, rather than assume a sleep caused an interleaving."""
    async with asyncio.timeout(5):
        async with session_maker() as observer:
            while True:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
                )).scalar_one()
                if blocker_pid in blockers:
                    return
                await asyncio.sleep(0.01)


async def _assert_accounting(org_id: int, balance: int, spent: int) -> None:
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == balance
        assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == balance
        assert await ledger.open_holds_of(db, org_id) == []
        assert await ledger.spent_today(db, org_id) == spent
        assert await ledger.spent_today_from_ledger(db, org_id) == spent


async def test_deferred_release_and_settle_can_finish_with_a_competing_settlement(postgres_money):
    """A mixed batch and an ordinary settle must both commit, without a lost charge or refund.

    Pause the batch after its release has acquired Org. Start the independent settlement and
    wait until PostgreSQL reports it blocked by the batch, then let the batch settle its second
    hold. With the old block-first settlement, the independent transaction already owns
    CreditBlock: this creates a real Org <-> CreditBlock deadlock. With Org-first settlement it
    waits before owning any block, so the batch can commit and both calls are accounted for.
    """
    org_id = await _funded_org("mixed-deferred")
    released = await _reserve(org_id, "mixed-release")
    batched = await _reserve(org_id, "mixed-settle")
    ordinary = await _reserve(org_id, "ordinary-settle")
    pending = [
        DeferredSettle(released, False, None, None, "not_billable", {}),
        DeferredSettle(batched, True, 80, None, "", {}),
    ]
    async def batch():
        return await close_deferred(pending, charge=True)

    async def ordinary_settle():
        async with session_maker() as db:
            return await ledger.settle(db, ordinary, 50)

    results = await _contend_after_statement(
        postgres_money, batch, ordinary_settle, "DELETE FROM TAGSPEND")
    assert results == [80, 50], results

    await _assert_accounting(org_id, balance=870, spent=130)
    async with session_maker() as db:
        entries = await ledger.entries_of(db, org_id)
        closed = [(entry.call_id, entry.kind, entry.amount_micro)
                  for entry in entries if entry.kind in {"settle", "release"}]
        assert sorted(closed) == sorted([
            (released, "release", 100), (batched, "settle", -80), (ordinary, "settle", -50),
        ])
        assert await ledger.spend_by_tag(db, org_id, "customer", datetime(2000, 1, 1)) == {
            batched: 80, ordinary: 50,
        }


async def _contend_after_statement(
    engine,
    first: Callable[[], Awaitable[int]],
    second: Callable[[], Awaitable[int]],
    pause_prefix: str,
) -> list:
    """Hold the first transaction at a SQL boundary until the second really waits for it."""
    paused = asyncio.Event()
    resume = asyncio.Event()
    first_pid = 0
    second_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    tasks = []

    async def pause(driver_connection):
        nonlocal first_pid
        first_pid = driver_connection.get_server_pid()
        paused.set()
        await resume.wait()

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        if (tasks and asyncio.current_task() is tasks[0] and not paused.is_set()
                and statement.lstrip().upper().startswith(pause_prefix)):
            conn.connection.dbapi_connection.run_async(pause)

    def before_statement(conn, cursor, statement, parameters, context, executemany):
        if len(tasks) > 1 and asyncio.current_task() is tasks[1] and not second_pid.done():
            second_pid.set_result(conn.connection.driver_connection.get_server_pid())

    event.listen(engine.sync_engine, "after_cursor_execute", after_statement)
    event.listen(engine.sync_engine, "before_cursor_execute", before_statement)
    try:
        async with asyncio.timeout(15):
            tasks.append(asyncio.create_task(first()))
            await paused.wait()
            tasks.append(asyncio.create_task(second()))
            await _wait_for_blocker(await second_pid, first_pid)
            resume.set()
            return await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        resume.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(engine.sync_engine, "after_cursor_execute", after_statement)
        event.remove(engine.sync_engine, "before_cursor_execute", before_statement)


async def test_opposite_hub_payments_complete_and_credit_each_payee_once(postgres_money):
    """Both directions must choose compatible locks, even when each refunds part of its hold.

    Before Org ordering, each direction takes its payer's block and Org, then needs the other's
    Org to credit its payee. Pause one after its first balance update and let the other block;
    proceeding creates that real cycle on the old implementation. An ordered implementation
    blocks the second payment before it can hold the other Org.
    """
    first_org = await _funded_org("hub-first")
    second_org = await _funded_org("hub-second")
    first_hold = await _reserve(first_org, "hub-first-to-second")
    second_hold = await _reserve(second_org, "hub-second-to-first")

    async def first_payment():
        async with session_maker() as db:
            result = await ledger.settle_to_in_transaction(db, first_hold, second_org, actual_micro=60)
            await db.commit()
            return result

    async def second_payment():
        async with session_maker() as db:
            result = await ledger.settle_to_in_transaction(db, second_hold, first_org, actual_micro=40)
            await db.commit()
            return result

    results = await _contend_after_statement(
        postgres_money, first_payment, second_payment, "UPDATE ORG ")
    assert results == [60, 40], results
    await _assert_accounting(first_org, balance=980, spent=60)
    await _assert_accounting(second_org, balance=1_020, spent=40)
    async with session_maker() as db:
        for org_id, paid_hold, received_hold, paid, received in (
            (first_org, first_hold, second_hold, 60, 40),
            (second_org, second_hold, first_hold, 40, 60),
        ):
            entries = await ledger.entries_of(db, org_id)
            movements = [(entry.call_id, entry.kind, entry.amount_micro)
                         for entry in entries if entry.call_id in {first_hold, second_hold}
                         and entry.kind != "reserve"]
            assert sorted(movements) == sorted([
                (paid_hold, "settle", -paid), (received_hold, "grant", received),
            ])
        # A retry after both commits must not credit either payee again.
        assert await ledger.settle_to_in_transaction(db, first_hold, second_org, actual_micro=60) == 0
        assert await ledger.settle_to_in_transaction(db, second_hold, first_org, actual_micro=40) == 0
        await db.commit()
    await _assert_accounting(first_org, balance=980, spent=60)
    await _assert_accounting(second_org, balance=1_020, spent=40)


@pytest.mark.parametrize("winner", ["settle", "release"])
async def test_settlement_and_release_claim_one_hold_exactly_once(postgres_money, winner):
    org_id = await _funded_org("same-hold")
    hold_id = await _reserve(org_id, "same-hold-race")

    async def operation(db, kind):
        if kind == "settle":
            return await ledger.settle_in_transaction(db, hold_id, 60)
        return await ledger.release_in_transaction(db, hold_id)

    async def first():
        async with session_maker() as db:
            result = await operation(db, winner)
            await db.commit()
            return result

    async def second():
        async with session_maker() as db:
            result = await operation(db, "release" if winner == "settle" else "settle")
            await db.commit()
            return result

    results = await _contend_after_statement(postgres_money, first, second, "DELETE FROM HOLD ")
    assert results == ([60, 0] if winner == "settle" else [100, 0]), results
    spent = 60 if winner == "settle" else 0
    await _assert_accounting(org_id, balance=1_000 - spent, spent=spent)
    async with session_maker() as db:
        closes = [entry for entry in await ledger.entries_of(db, org_id)
                  if entry.call_id == hold_id and entry.kind in {"settle", "release"}]
        assert [(entry.kind, entry.amount_micro) for entry in closes] == [
            (winner, -60 if winner == "settle" else 100),
        ]
        assert await ledger.tag_invoice_since(
            db, org_id, "customer", hold_id, datetime(2000, 1, 1)) == spent


async def test_deferred_failure_rolls_back_every_child_and_can_be_retried(postgres_money):
    org_id = await _funded_org("batch-rollback")
    released = await _reserve(org_id, "rollback-release")
    first = await _reserve(org_id, "rollback-first")
    second = await _reserve(org_id, "rollback-second")
    items = [
        DeferredSettle(released, False, None, None, "not_billable", {}),
        DeferredSettle(first, True, 80, None, "", {}),
        DeferredSettle(second, True, 50, None, "", {}),
    ]
    updates = 0

    def fail_after_second_settlement(conn, cursor, statement, parameters, context, executemany):
        nonlocal updates
        if statement.lstrip().upper().startswith("UPDATE TAGSPEND "):
            updates += 1
            if updates == 2:
                # A real PostgreSQL transaction error after money SQL has executed. The whole
                # batch, including the earlier refund, must roll back rather than partly commit.
                conn.exec_driver_sql("SELECT 1 / 0")

    event.listen(postgres_money.sync_engine, "after_cursor_execute", fail_after_second_settlement)
    try:
        assert await close_deferred(list(items), charge=True) == 0
    finally:
        event.remove(postgres_money.sync_engine, "after_cursor_execute", fail_after_second_settlement)
    assert updates == 2
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 700
        assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == 1_000
        assert sorted(hold.id for hold in await ledger.open_holds_of(db, org_id)) == sorted([
            released, first, second,
        ])
        assert await ledger.spent_today(db, org_id) == 300
        assert await ledger.spent_today_from_ledger(db, org_id) == 300
        assert not [entry for entry in await ledger.entries_of(db, org_id)
                    if entry.kind in {"settle", "release"}]
        assert await ledger.spend_by_tag(db, org_id, "customer", datetime(2000, 1, 1)) == {}
        for hold_id in (released, first, second):
            assert await ledger.tag_spent_since(
                db, org_id, "customer", hold_id, datetime(2000, 1, 1)) == 100
    assert await close_deferred(list(items), charge=True) == 130
    await _assert_accounting(org_id, balance=870, spent=130)


async def test_batches_with_opposite_org_orders_both_commit(postgres_money):
    first_org = await _funded_org("batch-first-org")
    second_org = await _funded_org("batch-second-org")
    first_charge = await _reserve(first_org, "first-org-charge")
    first_release = await _reserve(first_org, "first-org-release")
    second_charge = await _reserve(second_org, "second-org-charge")
    second_release = await _reserve(second_org, "second-org-release")

    async def first_batch():
        return await close_deferred([
            DeferredSettle(second_release, False, None, None, "not_billable", {}),
            DeferredSettle(first_charge, True, 80, None, "", {}),
        ], charge=True)

    async def second_batch():
        return await close_deferred([
            DeferredSettle(first_release, False, None, None, "not_billable", {}),
            DeferredSettle(second_charge, True, 50, None, "", {}),
        ], charge=True)

    results = await _contend_after_statement(
        postgres_money, first_batch, second_batch, "DELETE FROM TAGSPEND")
    assert results == [80, 50], results
    await _assert_accounting(first_org, balance=920, spent=80)
    await _assert_accounting(second_org, balance=950, spent=50)
    async with session_maker() as db:
        for org_id, charged, released, amount in (
            (first_org, first_charge, first_release, 80),
            (second_org, second_charge, second_release, 50),
        ):
            closes = [(entry.call_id, entry.kind, entry.amount_micro)
                      for entry in await ledger.entries_of(db, org_id)
                      if entry.kind in {"settle", "release"}]
            assert sorted(closes) == sorted([
                (charged, "settle", -amount), (released, "release", 100),
            ])


async def test_global_reaper_lost_claim_cannot_deadlock_a_cross_org_payment(postgres_money):
    """A lost claim does not commit, so the sweep must not retain Org locks in reverse order.

    Let the reaper read two stale holds, then close its oldest one elsewhere. Pause its lost
    DELETE while a payment from the lower Org to the higher Org starts. Org-first claims without
    ordered sweep processing make a cycle here: the lost claim retains the higher Org lock,
    while the payment holds the lower one. No fake money result or transaction is substituted.
    """
    lower = await _funded_org("reaper-lower")
    higher = await _funded_org("reaper-higher")
    lower_stale = await _reserve(lower, "lower-stale")
    higher_stale = await _reserve(higher, "higher-stale")
    payment_hold = await _reserve(lower, "reaper-competing-payment")
    async with session_maker() as db:
        # The higher Org is the oldest: the candidate limit must still choose the oldest holds,
        # even if their processing order differs. Leave the concurrent payment's hold fresh.
        (await db.get(Hold, higher_stale)).created_at = datetime(2000, 1, 1)
        (await db.get(Hold, lower_stale)).created_at = datetime(2000, 1, 1) + timedelta(seconds=1)
        await db.commit()

    selected = asyncio.Event()
    continue_selection = asyncio.Event()
    lost_claim = asyncio.Event()
    continue_sweep = asyncio.Event()
    sweep_pid = 0
    payment_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    tasks = []

    async def pause_selection(driver_connection):
        selected.set()
        await continue_selection.wait()

    async def pause_lost_claim(driver_connection):
        nonlocal sweep_pid
        sweep_pid = driver_connection.get_server_pid()
        lost_claim.set()
        await continue_sweep.wait()

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        if not tasks or asyncio.current_task() is not tasks[0]:
            return
        sql = statement.upper()
        if "ORDER BY HOLD.CREATED_AT" in sql and "LIMIT" in sql and not selected.is_set():
            conn.connection.dbapi_connection.run_async(pause_selection)
        elif (sql.startswith("DELETE FROM HOLD ") and higher_stale in parameters
              and not lost_claim.is_set()):
            conn.connection.dbapi_connection.run_async(pause_lost_claim)

    def before_statement(conn, cursor, statement, parameters, context, executemany):
        if len(tasks) > 1 and asyncio.current_task() is tasks[1] and not payment_pid.done():
            payment_pid.set_result(conn.connection.driver_connection.get_server_pid())

    async def reap():
        async with session_maker() as db:
            return await ledger.reap_stale_holds(db, limit=2)

    async def payment():
        async with session_maker() as db:
            result = await ledger.settle_to_in_transaction(db, payment_hold, higher, actual_micro=60)
            await db.commit()
            return result

    event.listen(postgres_money.sync_engine, "after_cursor_execute", after_statement)
    event.listen(postgres_money.sync_engine, "before_cursor_execute", before_statement)
    try:
        async with asyncio.timeout(15):
            tasks.append(asyncio.create_task(reap()))
            await selected.wait()
            async with session_maker() as winner:
                assert await ledger.release(winner, higher_stale) == 100
            continue_selection.set()
            await lost_claim.wait()
            tasks.append(asyncio.create_task(payment()))
            pid = await payment_pid
            # The block-first implementation retains no Org lock after a lost DELETE, so its
            # payment may already finish. Both that behavior and an ordered Org-first wait are
            # valid; only a real circular wait is a regression.
            async with asyncio.timeout(5):
                async with session_maker() as observer:
                    while not tasks[1].done():
                        blockers = (await observer.execute(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                        )).scalar_one()
                        if sweep_pid in blockers:
                            break
                        await asyncio.sleep(0.01)
            continue_sweep.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        assert results == [2, 60], results
    finally:
        continue_selection.set()
        continue_sweep.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(postgres_money.sync_engine, "after_cursor_execute", after_statement)
        event.remove(postgres_money.sync_engine, "before_cursor_execute", before_statement)

    async with session_maker() as db:
        for org_id, expected, stale in ((lower, 940, lower_stale), (higher, 1_060, higher_stale)):
            assert await ledger.balance_of(db, org_id) == expected
            assert sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id)) == expected
            assert await ledger.open_holds_of(db, org_id) == []
            releases = [entry for entry in await ledger.entries_of(db, org_id)
                        if entry.kind == "release" and entry.call_id == stale]
            assert len(releases) == 1 and releases[0].amount_micro == 100


@pytest.mark.parametrize("funding", ["grant", "topup"])
async def test_funding_can_complete_while_an_unrelated_foreign_key_holds_key_share(
    postgres_money, funding,
):
    """Funding must not upgrade the Org lock so far that unrelated FK inserts block it."""
    org_id = await _funded_org("funding-key-share")
    async with session_maker() as unrelated:
        # PostgreSQL's FK check takes KEY SHARE on Org until this transaction ends. The staged
        # conversion is unrelated to money and remains uncommitted while the funding completes.
        unrelated.add(AdConversion(org_id=org_id, action="uncommitted-conversion"))
        await unrelated.flush()
        async with asyncio.timeout(3):
            async with session_maker() as db:
                if funding == "topup":
                    await ledger.topup(db, org_id, 250, "funding-key-share-payment")
                else:
                    await ledger.grant(db, org_id, amount_micro=250, once=False)
                await db.commit()
        await _assert_accounting(org_id, balance=1_250, spent=0)
        await unrelated.rollback()


async def test_one_org_transaction_does_not_block_another_org_settlement(postgres_money):
    blocked_org = await _funded_org("blocked-org")
    independent_org = await _funded_org("independent-org")
    blocked_hold = await _reserve(blocked_org, "blocked-org-hold")
    independent_hold = await _reserve(independent_org, "independent-org-hold")
    async with session_maker() as blocked:
        # Leave this legitimate money transaction open, holding Org and Hold until rollback.
        assert await ledger.release_in_transaction(blocked, blocked_hold) == 100
        async with asyncio.timeout(3):
            async with session_maker() as db:
                assert await ledger.settle(db, independent_hold, 60) == 60
        await _assert_accounting(independent_org, balance=940, spent=60)
        await blocked.rollback()
    async with session_maker() as db:
        assert await ledger.release(db, blocked_hold) == 100
    await _assert_accounting(blocked_org, balance=1_000, spent=0)


@pytest.mark.parametrize("closing", ["settle", "release"])
async def test_caller_can_close_a_reservation_staged_in_the_same_transaction(postgres_money, closing):
    """The lock helper's no-autoflush scope must not hide a newly staged Hold from its closer."""
    org_id = await _funded_org("staged-hold")
    async with session_maker() as db:
        hold_id = await ledger.reserve_in_transaction(
            db, org_id, "test.staged", 100, call_id="unflushed-hold",
            tags={"customer": "staged"},
        )
        # No intervening SELECT, flush or commit: use the same public application-owned boundary.
        if closing == "settle":
            assert await ledger.settle_in_transaction(db, hold_id, 60) == 60
        else:
            assert await ledger.release_in_transaction(db, hold_id) == 100
        await db.commit()
    spent = 60 if closing == "settle" else 0
    await _assert_accounting(org_id, balance=1_000 - spent, spent=spent)
    async with session_maker() as db:
        closes = [entry for entry in await ledger.entries_of(db, org_id)
                  if entry.call_id == hold_id and entry.kind in {"settle", "release"}]
        assert len(closes) == 1 and closes[0].kind == closing
        assert await ledger.tag_invoice_since(
            db, org_id, "customer", "staged", datetime(2000, 1, 1)) == spent
