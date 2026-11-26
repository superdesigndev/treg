"""A team deleted while one of its calls is still writing leaves none of that call's rows behind,
for the tables that name the team with no foreign key. Against real PostgreSQL only: the guarantee
rests on its row locks.

Opt in with TREG_TEST_DB_URL naming a disposable local database.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update

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
