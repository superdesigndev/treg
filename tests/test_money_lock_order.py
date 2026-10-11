"""Money's concurrent transaction contract, exercised against real PostgreSQL locks.

Run with TREG_TEST_DB_URL pointing at an isolated test database. SQLite cannot exercise these
waits. SQLAlchemy events pause SQL at the database boundary; no money operation is mocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

import pytest
from sqlalchemy import event, select, text

from treg.application.call.settle import DeferredSettle, close_deferred
from treg.config import get_settings
from treg.domain import money as ledger, referrals
from treg.infra.db import reset_db, session_maker
from treg.models import AdConversion, ArchiveKeyOrg, CreditBlock, Membership, Org, Referral, User


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
    CreditBlock: this creates a real Org <-> CreditBlock deadlock. A compatible batch protocol must let both operations complete without losing a
    charge or refund; it need not serialize every ordinary settlement on Org.
    """
    org_id = await _funded_org("mixed-deferred")
    released = await _reserve(org_id, "mixed-release")
    batched = await _reserve(org_id, "mixed-settle")
    ordinary = await _reserve(org_id, "ordinary-settle")
    pending = [
        DeferredSettle(released, False, None, None, "not_billable", {}, payer_org_id=org_id),
        DeferredSettle(batched, True, 80, None, "", {}, payer_org_id=org_id),
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
    *, while_paused: Callable[[], Awaitable[None]] | None = None,
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
            if while_paused is not None:
                await while_paused()
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
    proceeding creates that real cycle on the old implementation. A compatible transfer protocol must let both directions commit, without requiring
    an Org lock for ordinary settlements that do not change the balance or daily counter.
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
        DeferredSettle(released, False, None, None, "not_billable", {}, payer_org_id=org_id),
        DeferredSettle(first, True, 80, (org_id, "rollback-first-key"), "", {}, payer_org_id=org_id),
        DeferredSettle(second, True, 50, (org_id, "rollback-second-key"), "", {}, payer_org_id=org_id),
    ]
    async with session_maker() as db:
        db.add_all([ArchiveKeyOrg(org_id=org_id, key_hash=key)
                    for key in ("rollback-first-key", "rollback-second-key")])
        await db.commit()
    updates = 0

    def fail_after_archive_write(conn, cursor, statement, parameters, context, executemany):
        nonlocal updates
        sql = statement.lstrip().upper()
        if sql.startswith(("UPDATE ARCHIVEKEYORG ", "INSERT INTO ARCHIVEKEYORG ")):
            updates += 1
            if updates == 1:
                # A real PostgreSQL transaction error after money SQL has executed. The whole
                # batch, including its refund and archive write, must roll back together.
                conn.exec_driver_sql("SELECT 1 / 0")

    event.listen(postgres_money.sync_engine, "after_cursor_execute", fail_after_archive_write)
    try:
        assert await close_deferred(list(items), charge=True) == 0
    finally:
        event.remove(postgres_money.sync_engine, "after_cursor_execute", fail_after_archive_write)
    assert updates == 1, "the injected database failure must have been reached"
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
        marks = (await db.execute(select(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == org_id))).scalars()
        assert {mark.key_hash: mark.calls for mark in marks} == {
            "rollback-first-key": 1, "rollback-second-key": 1,
        }
        assert await ledger.spend_by_tag(db, org_id, "customer", datetime(2000, 1, 1)) == {}
        for hold_id in (released, first, second):
            assert await ledger.tag_spent_since(
                db, org_id, "customer", hold_id, datetime(2000, 1, 1)) == 100
    assert await close_deferred(list(items), charge=True) == 130
    await _assert_accounting(org_id, balance=870, spent=130)
    async with session_maker() as db:
        marks = (await db.execute(select(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == org_id))).scalars()
        assert {mark.key_hash: mark.calls for mark in marks} == {
            "rollback-first-key": 2, "rollback-second-key": 2,
        }


async def test_batches_with_opposite_org_orders_both_commit(postgres_money):
    first_org = await _funded_org("batch-first-org")
    second_org = await _funded_org("batch-second-org")
    first_charge = await _reserve(first_org, "first-org-charge")
    first_release = await _reserve(first_org, "first-org-release")
    second_charge = await _reserve(second_org, "second-org-charge")
    second_release = await _reserve(second_org, "second-org-release")

    async def first_batch():
        return await close_deferred([
            DeferredSettle(second_release, False, None, None, "not_billable", {}, payer_org_id=second_org),
            DeferredSettle(first_charge, True, 80, None, "", {}, payer_org_id=first_org),
        ], charge=True)

    async def second_batch():
        return await close_deferred([
            DeferredSettle(first_release, False, None, None, "not_billable", {}, payer_org_id=first_org),
            DeferredSettle(second_charge, True, 50, None, "", {}, payer_org_id=second_org),
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
    """A hold staged by the caller can be closed before it has been explicitly flushed."""
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


async def test_exact_cost_settlement_does_not_wait_for_another_hold_refund(postgres_money):
    """A same-day exact-cost settle changes blocks and tags, but needs no Org write."""
    org_id = await _funded_org("exact-cost")
    refund = await _reserve(org_id, "exact-cost-refund")
    charged = await _reserve(org_id, "exact-cost-charge")
    async with session_maker() as blocker:
        assert await ledger.release_in_transaction(blocker, refund) == 100
        async with asyncio.timeout(3):
            async with session_maker() as db:
                assert await ledger.settle(db, charged, 100) == 100
        await blocker.rollback()
    async with session_maker() as db:
        assert await ledger.release(db, refund) == 100
    await _assert_accounting(org_id, balance=900, spent=100)


async def test_batch_waiting_for_an_ordinary_claim_does_not_keep_a_refund_lock(postgres_money):
    """Claim all batch holds before money writes, or Hold -> Org can become a new cycle."""
    org_id = await _funded_org("batch-overlapping-claim")
    refund = await _reserve(org_id, "a-batch-refund")
    raced = await _reserve(org_id, "z-ordinary-claim")

    async def ordinary():
        async with session_maker() as db:
            return await ledger.settle(db, raced, 60)

    async def batch():
        return await close_deferred([
            DeferredSettle(refund, False, None, None, "not_billable", {}, payer_org_id=org_id),
            DeferredSettle(raced, True, 80, None, "", {}, payer_org_id=org_id),
        ], charge=True)

    results = await _contend_after_statement(postgres_money, ordinary, batch, "DELETE FROM HOLD ")
    assert results == [60, 0], results
    await _assert_accounting(org_id, balance=940, spent=60)
    async with session_maker() as db:
        closes = [(entry.call_id, entry.kind, entry.amount_micro)
                  for entry in await ledger.entries_of(db, org_id)
                  if entry.kind in {"settle", "release"}]
        assert sorted(closes) == sorted([(refund, "release", 100), (raced, "settle", -60)])


async def test_overlapping_batches_claim_holds_once_despite_opposite_input_order(postgres_money):
    org_id = await _funded_org("batch-overlapping-order")
    first = await _reserve(org_id, "a-overlapping-hold")
    second = await _reserve(org_id, "z-overlapping-hold")

    async def first_batch():
        return await close_deferred([
            DeferredSettle(second, False, None, None, "not_billable", {}, payer_org_id=org_id),
            DeferredSettle(first, True, 70, None, "", {}, payer_org_id=org_id),
        ], charge=True)

    async def second_batch():
        return await close_deferred([
            DeferredSettle(first, False, None, None, "not_billable", {}, payer_org_id=org_id),
            DeferredSettle(second, True, 80, None, "", {}, payer_org_id=org_id),
        ], charge=True)

    results = await _contend_after_statement(
        postgres_money, first_batch, second_batch, "DELETE FROM HOLD ")
    assert results == [70, 0], results
    await _assert_accounting(org_id, balance=930, spent=70)
    async with session_maker() as db:
        closes = [(entry.call_id, entry.kind, entry.amount_micro)
                  for entry in await ledger.entries_of(db, org_id)
                  if entry.kind in {"settle", "release"}]
        assert sorted(closes) == sorted([(first, "settle", -70), (second, "release", 100)])


async def test_batch_does_not_lock_a_newly_funded_block_after_updating_org(postgres_money):
    """A block inserted between the batch's read and its Org writes cannot enter its lock set.

    The existing block sorts after every generated funding ID. The ordinary settlement first
    locks the newly committed block, then waits for the existing block held by the batch. If the
    batch queries blocks again after refunding its first hold, it closes a real wait cycle.
    """
    async with session_maker() as db:
        org = Org(name="concurrent-funding", slug="concurrent-funding", balance_micro=1_000)
        db.add(org)
        await db.flush()
        org_id = org.id
        assert org_id is not None
        db.add(CreditBlock(id="zz-existing-block", org_id=org_id, kind="promotional",
                           amount_micro=1_000, remaining_micro=1_000))
        await db.commit()
    first = await _reserve(org_id, "funding-batch-first")
    second = await _reserve(org_id, "funding-batch-second")
    ordinary_hold = await _reserve(org_id, "funding-ordinary")

    async def batch():
        return await close_deferred([
            DeferredSettle(first, True, 80, None, "", {}, payer_org_id=org_id),
            DeferredSettle(second, True, 50, None, "", {}, payer_org_id=org_id),
        ], charge=True)

    async def fund_between_block_lock_and_balance_write():
        async with session_maker() as db:
            await ledger.topup(db, org_id, 250, "concurrent-funding-payment")
            await db.commit()
            blocks = await ledger.blocks_of(db, org_id)
            assert len(blocks) == 2
            assert next(b.id for b in blocks if b.kind == "purchased") < "zz-existing-block"

    async def ordinary():
        async with session_maker() as db:
            return await ledger.settle(db, ordinary_hold, 60)

    results = await _contend_after_statement(
        postgres_money, batch, ordinary, "SELECT CREDITBLOCK.",
        while_paused=fund_between_block_lock_and_balance_write)
    assert results == [130, 60], results
    await _assert_accounting(org_id, balance=1_060, spent=190)
    async with session_maker() as db:
        assert {b.kind: b.remaining_micro for b in await ledger.blocks_of(db, org_id)} == {
            "promotional": 810, "purchased": 250,
        }


async def test_zero_cost_batches_record_archive_use_in_compatible_order(postgres_money):
    """Archive marks still need a compatible order when zero-cost holds take no money locks."""
    org_id = await _funded_org("zero-cost-archive")
    async with session_maker() as db:
        db.add_all([ArchiveKeyOrg(org_id=org_id, key_hash=key) for key in ("a-key", "z-key")])
        await db.commit()
        holds = [await ledger.reserve(db, org_id, "test.archive", 0, call_id=f"archive-{i}")
                 for i in range(4)]

    async def first_batch():
        return await close_deferred([
            DeferredSettle(holds[0], True, 0, (org_id, "z-key"), "", {}, payer_org_id=org_id),
            DeferredSettle(holds[1], True, 0, (org_id, "a-key"), "", {}, payer_org_id=org_id),
        ], charge=True)

    async def second_batch():
        return await close_deferred([
            DeferredSettle(holds[2], True, 0, (org_id, "a-key"), "", {}, payer_org_id=org_id),
            DeferredSettle(holds[3], True, 0, (org_id, "z-key"), "", {}, payer_org_id=org_id),
        ], charge=True)

    results = await _contend_after_statement(
        postgres_money, first_batch, second_batch, "INSERT INTO ARCHIVEKEYORG ")
    assert results == [0, 0], results
    await _assert_accounting(org_id, balance=1_000, spent=0)
    async with session_maker() as db:
        marks = (await db.execute(select(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == org_id))).scalars()
        assert {mark.key_hash: mark.calls for mark in marks} == {"a-key": 3, "z-key": 3}
        assert len([e for e in await ledger.entries_of(db, org_id) if e.kind == "settle"]) == 4


@pytest.mark.parametrize("closing", ["single", "batch"])
async def test_settlement_refreshes_blocks_read_before_another_committed_charge(
    postgres_money, closing,
):
    org_id = await _funded_org("stale-block-read")
    earlier = await _reserve(org_id, "stale-block-earlier")
    later = await _reserve(org_id, "stale-block-later")
    async with session_maker() as stale_reader:
        # Keep the ORM reference alive. PostgreSQL READ COMMITTED can see the newer row while
        # SQLAlchemy's identity map still has an older balance unless the locked read refreshes it.
        previously_read = await ledger.blocks_of(stale_reader, org_id)
        assert sum(block.remaining_micro for block in previously_read) == 1_000
        async with session_maker() as concurrent:
            assert await ledger.settle(concurrent, earlier, 70) == 70
        assert sum(block.remaining_micro for block in previously_read) == 1_000
        if closing == "batch":
            assert await ledger.close_holds_in_transaction(stale_reader, [
                ledger.HoldClose(later, True, 80),
            ]) == [80]
        else:
            assert await ledger.settle_in_transaction(stale_reader, later, 80) == 80
        await stale_reader.commit()
    await _assert_accounting(org_id, balance=850, spent=150)


async def test_repeated_hold_in_one_batch_uses_first_instruction_and_returns_input_order(postgres_money):
    org_id = await _funded_org("duplicate-batch-hold")
    first = await _reserve(org_id, "z-first-instruction")
    second = await _reserve(org_id, "a-second-instruction")
    closings = [
        ledger.HoldClose(first, True, 70),
        ledger.HoldClose(second, False, reason="not_billable"),
        ledger.HoldClose(first, False, reason="duplicate"),
        ledger.HoldClose("missing-hold", True, 40),
    ]
    async with session_maker() as db:
        assert await ledger.close_holds_in_transaction(db, closings) == [70, 100, 0, 0]
        await db.commit()
        assert await ledger.close_holds_in_transaction(db, closings) == [0, 0, 0, 0]
        await db.commit()
    await _assert_accounting(org_id, balance=930, spent=70)
    async with session_maker() as db:
        closes = [(entry.call_id, entry.kind, entry.amount_micro)
                  for entry in await ledger.entries_of(db, org_id)
                  if entry.kind in {"settle", "release"}]
        assert sorted(closes) == sorted([(first, "settle", -70), (second, "release", 100)])


async def test_opposite_referral_payouts_credit_both_sides_once(postgres_money, monkeypatch):
    monkeypatch.setattr(get_settings(), "referral_referred_micro", 30)
    monkeypatch.setattr(get_settings(), "referral_referrer_micro", 50)
    first_org = await _funded_org("referral-first")
    second_org = await _funded_org("referral-second")
    async with session_maker() as db:
        first_user = User(email="referral-first@example.test")
        second_user = User(email="referral-second@example.test")
        db.add_all([first_user, second_user])
        await db.flush()
        first_id, second_id = first_user.id, second_user.id
        assert first_id is not None and second_id is not None
        db.add_all([
            Membership(user_id=first_id, org_id=first_org, role="owner", token_hash="first"),
            Membership(user_id=second_id, org_id=second_org, role="owner", token_hash="second"),
            Referral(code="first-code", referrer_user_id=first_id, referred_user_id=second_id,
                     referred_org_id=second_org, status="qualified", qualified_at=datetime(2000, 1, 1)),
            Referral(code="second-code", referrer_user_id=second_id, referred_user_id=first_id,
                     referred_org_id=first_org, status="qualified", qualified_at=datetime(2000, 1, 1)),
        ])
        await db.commit()

    async def first_payout():
        async with session_maker() as db:
            return await referrals.sweep(db, referrer_user_id=first_id)

    async def second_payout():
        async with session_maker() as db:
            return await referrals.sweep(db, referrer_user_id=second_id)

    results = await _contend_after_statement(
        postgres_money, first_payout, second_payout, "UPDATE ORG ")
    assert results == [1, 1], results
    await _assert_accounting(first_org, balance=1_080, spent=0)
    await _assert_accounting(second_org, balance=1_080, spent=0)
    async with session_maker() as db:
        assert await referrals.sweep(db) == 0
        rows = (await db.execute(select(Referral))).scalars().all()
        assert len(rows) == 2
        assert all(row.status == "paid" and row.referrer_reward_micro == 50
                   and row.referred_reward_micro == 30 and row.referrer_block_id
                   and row.referred_block_id for row in rows)
        for org_id in (first_org, second_org):
            grants = [entry for entry in await ledger.entries_of(db, org_id)
                      if entry.kind == "grant" and entry.meta.get("block_kind") == "referral"]
            assert sorted(entry.amount_micro for entry in grants) == [30, 50]
