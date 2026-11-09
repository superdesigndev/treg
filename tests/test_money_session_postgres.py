"""A money transaction whose process stops running gives its row locks back after a bounded idle."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from treg.infra import money_session as money_session_module
from treg.infra.db import reset_db, session_maker
from treg.infra.money_session import money_session
from treg.models import Org


@pytest.fixture
async def postgres():
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await reset_db()
    async with session_maker() as db:
        org = Org(name="idle-holder", slug="idle-holder", balance_micro=1_000)
        db.add(org)
        await db.commit()
        return org.id


async def _timeout(db) -> str:
    return (await db.execute(text("SHOW idle_in_transaction_session_timeout"))).scalar_one()


async def test_only_money_transactions_carry_the_idle_bound(postgres):
    async with money_session(session_maker()) as db:
        assert await _timeout(db) == money_session_module.IDLE_IN_TRANSACTION_TIMEOUT
        await db.commit()
        assert await _timeout(db) == money_session_module.IDLE_IN_TRANSACTION_TIMEOUT
    async with session_maker() as db:
        assert await _timeout(db) == "0"


async def test_a_stalled_holder_frees_the_balance_row_for_the_next_writer(postgres, monkeypatch):
    org_id = postgres
    monkeypatch.setattr(money_session_module, "IDLE_IN_TRANSACTION_TIMEOUT", "200ms")
    async with money_session(session_maker()) as stalled:
        await stalled.execute(update(Org).where(Org.id == org_id).values(balance_micro=Org.balance_micro - 100))
        await asyncio.sleep(0.5)   # past the bound, holding the row
        async with asyncio.timeout(2), session_maker() as other:
            await other.execute(update(Org).where(Org.id == org_id).values(balance_micro=Org.balance_micro - 1))
            await other.commit()
        with pytest.raises(DBAPIError):
            await stalled.commit()
    async with session_maker() as db:
        assert (await db.execute(select(Org.balance_micro).where(Org.id == org_id))).scalar_one() == 999
