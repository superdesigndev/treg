"""A team's allowance: one hold that stands behind many calls, and the charges drawn against it."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import reset_db, session_maker
from treg.models import Hold, Org

GRANT = 1_000_000


@pytest.fixture
async def org_id():
    await reset_db()
    get_settings().platform_margin = 0
    async with session_maker() as db:
        org = Org(id=int(uuid4().hex[:7], 16) + 1, name="allowance", slug=uuid4().hex)
        db.add(org)
        await db.flush()
        await ledger.grant(db, org.id, amount_micro=GRANT)
        await db.commit()
        return org.id


async def _invariant(org_id: int) -> int:
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        blocks = sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id))
        holds = sum(h.amount_micro for h in await ledger.open_holds_of(db, org_id))
    assert balance == blocks - holds, f"balance {balance} != blocks {blocks} - holds {holds}"
    return balance


async def test_a_per_call_close_and_the_stale_sweep_leave_an_allowance_hold_alone(org_id):
    async with session_maker() as db:
        db.add(Hold(id="allowance-row", org_id=org_id, amount_micro=40_000, allowance=True,
                    created_at=ledger._now() - timedelta(seconds=ledger.hold_ttl_s() + 5)))
        await db.execute(update(Org).where(Org.id == org_id)
                         .values(balance_micro=Org.balance_micro - 40_000))
        await db.commit()
    async with session_maker() as db:
        assert await ledger.release(db, "allowance-row") == 0
        assert await ledger.settle(db, "allowance-row", 10) == 0
        assert await ledger.reap_stale_holds(db, org_id=org_id) == 0
    assert await _invariant(org_id) == GRANT - 40_000
