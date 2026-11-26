"""A team deleted while one of its calls is still writing leaves none of that call's rows behind,
for the tables that name the team with no foreign key. Against real PostgreSQL only: the guarantee
rests on its row locks.

Opt in with TREG_TEST_DB_URL naming a disposable local database.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update

from treg.domain.governance import teams
from treg.infra.db import reset_db, session_maker
from treg.models import Hold, Org

_LOCAL = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
async def postgres():
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires a disposable PostgreSQL database (TREG_TEST_DB_URL)")
    assert engine.url.host in _LOCAL, "local databases only"
    await reset_db()


async def _team() -> int:
    async with session_maker() as db:
        org = Org(name="race", slug=f"race-{uuid4().hex}", balance_micro=1_000_000)
        db.add(org)
        await db.commit()
        return org.id


async def _waiting_on_a_lock(pid: int) -> None:
    async with session_maker() as db:
        for _ in range(200):
            waiting = (await db.execute(text(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid})).scalar()
            if waiting:
                return
            await asyncio.sleep(0.02)
    raise AssertionError("the deletion never waited on the call's lock on the team row")


async def _holds(org_id: int) -> int:
    async with session_maker() as db:
        return (await db.execute(
            select(func.count()).select_from(Hold).where(Hold.org_id == org_id))).scalar_one()


@pytest.mark.parametrize("sweep", [True, False], ids=["swept", "without-the-sweep"])
async def test_a_hold_committed_while_its_team_is_deleted_goes_with_the_team(
        postgres, monkeypatch, sweep):
    if not sweep:
        monkeypatch.setattr(teams, "ORG_ROWS_WITHOUT_FOREIGN_KEY", ())
    org_id = await _team()

    # A reserve: it debits the team row, then writes its hold, and has not committed yet.
    call = session_maker()
    await call.execute(update(Org).where(Org.id == org_id)
                       .values(balance_micro=Org.balance_micro - 1))
    call.add(Hold(id=uuid4().hex, org_id=org_id, endpoint_id="test.race", amount_micro=1))
    await call.flush()

    async def delete_team() -> None:
        async with session_maker() as db:
            pid.set_result((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            await teams.cascade_delete_org(await db.get(Org, org_id), db)
            await db.commit()

    pid: asyncio.Future = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(delete_team())
    # The walk has passed `hold` (the call's row was not committed when it read it) and the
    # DELETE of the team row waits on the call's debit.
    await _waiting_on_a_lock(await pid)
    await call.commit()
    await call.close()
    await asyncio.wait_for(deletion, 10)

    async with session_maker() as db:
        assert await db.get(Org, org_id) is None
    assert await _holds(org_id) == (0 if sweep else 1)


@pytest.mark.parametrize("sweep", [True, False], ids=["swept", "without-the-sweep"])
async def test_a_settle_that_claimed_its_hold_before_the_deletion_leaves_no_ledger_entry(
        postgres, monkeypatch, sweep):
    """Settled at exactly what it reserved, the settle never touches the team row: only its claim
    on the hold, which the deletion's walk also deletes, makes the deletion wait for it."""
    from treg.config import get_settings
    from treg.domain import money
    from treg.models import LedgerEntry

    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    if not sweep:
        monkeypatch.setattr(teams, "ORG_ROWS_WITHOUT_FOREIGN_KEY", ())
    org_id = await _team()
    async with session_maker() as db:
        await money.grant(db, org_id, amount_micro=1_000_000)
        await db.commit()
        call_id = await money.reserve(db, org_id, "test.race", 1_000)

    call = session_maker()
    assert await money.settle_in_transaction(call, call_id, 1_000) == 1_000
    await call.flush()

    async def delete_team() -> None:
        async with session_maker() as db:
            pid.set_result((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            await teams.cascade_delete_org(await db.get(Org, org_id), db)
            await db.commit()

    pid: asyncio.Future = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(delete_team())
    # The walk has passed `ledgerentry` and waits on the hold the settle claimed.
    await _waiting_on_a_lock(await pid)
    await call.commit()
    await call.close()
    await asyncio.wait_for(deletion, 10)

    async with session_maker() as db:
        left = (await db.execute(select(func.count()).select_from(LedgerEntry)
                                 .where(LedgerEntry.org_id == org_id))).scalar_one()
    assert left == (0 if sweep else 1)


async def _deletion_done_or_waiting(deletion: asyncio.Task, pid: int) -> None:
    async with session_maker() as db:
        for _ in range(200):
            if deletion.done():
                return
            waiting = (await db.execute(text(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid})).scalar()
            if waiting:
                return
            await asyncio.sleep(0.02)
    raise AssertionError("the deletion neither ended nor waited")


@pytest.mark.parametrize("amount, team_lock", [(0, True), (1_000, True), (0, False)],
                         ids=["unpaid-charge", "paid-charge", "unpaid-charge-plain-read"])
async def test_a_charge_queued_after_the_deletion_read_the_queue_leaves_nothing_behind(
        postgres, monkeypatch, amount, team_lock):
    """A team with no allowance hold, so neither the charge writer nor the fold locks anything
    of the team's before the fold's own check. The charge is queued after the deletion's walk
    read `callcharge`, and the fold writes its settle entry and tag row while the deletion waits
    to delete the team row."""
    from treg.config import get_settings
    from treg.domain.money import allowance
    from treg.models import LedgerEntry, TagSpend

    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    if not team_lock:
        async def plain_read(db, org_id):
            return await allowance.team_exists(db, org_id)
        monkeypatch.setattr(allowance, "_lock_team", plain_read)
    org_id = await _team()
    async with session_maker() as db:
        await money_grant(db, org_id)

    # Something else holds the team row, so the deletion stops just before deleting it, its
    # walk done.
    blocker = session_maker()
    await blocker.execute(select(Org.id).where(Org.id == org_id).with_for_update(key_share=True))

    pid: asyncio.Future = asyncio.get_running_loop().create_future()

    async def delete_team() -> None:
        async with session_maker() as db:
            pid.set_result((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            await teams.cascade_delete_org(await db.get(Org, org_id), db)
            await db.commit()

    deletion = asyncio.create_task(delete_team())
    await _waiting_on_a_lock(await pid)

    async with session_maker() as db:
        await allowance.record_charges(db, [allowance.Charge(
            call_id=uuid4().hex, org_id=org_id, endpoint_id="test.race", actual_micro=amount,
            meta={"tags": {"customer": "c-1"}})])
        await db.commit()

    fold_session = session_maker()
    folded = asyncio.Event()
    statements: list[str] = []

    def seen(conn, cursor, statement, *args):
        if conn.info.get("fold"):
            statements.append(statement)

    engine = session_maker.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", seen)
    (await fold_session.connection()).info["fold"] = True

    async def fold() -> None:
        await allowance.fold_charges_in_transaction(fold_session, org_id)
        await fold_session.flush()
        folded.set()

    folding = asyncio.create_task(fold())
    await asyncio.sleep(0.2)  # a paid charge waits here on the blocks the deletion deleted
    await blocker.commit()
    await blocker.close()
    await _deletion_done_or_waiting(deletion, await pid)
    await asyncio.wait_for(folding, 10)
    await fold_session.commit()
    await fold_session.close()
    await asyncio.wait_for(deletion, 10)
    event.remove(engine, "before_cursor_execute", seen)
    if team_lock:
        # The fold shares the team row with balance writes (FOR NO KEY UPDATE) instead of
        # queueing behind them.
        on_team = [s for s in statements if "FROM org" in s and " FOR " in s]
        assert on_team and all(s.rstrip().endswith("FOR KEY SHARE") for s in on_team), on_team

    async with session_maker() as db:
        left = [(await db.execute(select(func.count()).select_from(model)
                                  .where(model.org_id == org_id))).scalar_one()
                for model in (LedgerEntry, TagSpend)]
    assert left == ([0, 0] if team_lock else [1, 1])


async def money_grant(db, org_id: int) -> None:
    from treg.domain import money

    await money.grant(db, org_id, amount_micro=1_000_000)
    await db.commit()
