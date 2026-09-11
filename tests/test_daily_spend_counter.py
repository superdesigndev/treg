"""`Org.spent_today_micro` is the daily cap's number and must agree with the journal.

The counter exists so the fail-closed daily cap costs one primary-key read on every metered call
instead of an aggregate over the platform's whole day (revision 0022). These tests pin the
semantics the journal view (`spent_today_from_ledger`) has always had: settled today plus still
held from today, reset at the UTC day boundary, with a hold opened yesterday belonging to yesterday.
"""
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from treg.api import app
from treg.domain import money as ledger
from treg.infra.db import reset_db, session_maker
from treg.models import Hold, Org
from conftest import make_upstream, verified_signup

EP = "acme.thing.get"


@pytest.fixture
async def c():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


async def _org(c: AsyncClient) -> int:
    r = await verified_signup(c, json={"email": "counter@superdesign.dev"})
    assert r.status_code == 200, r.text
    return r.json()["org_id"]


async def _both(org_id: int) -> tuple[int, int]:
    """(counter, journal) — every assertion below checks them against each other too."""
    async with session_maker() as db:
        return await ledger.spent_today(db, org_id), await ledger.spent_today_from_ledger(db, org_id)


async def test_counter_follows_reserve_settle_and_release(c: AsyncClient):
    org_id = await _org(c)
    assert await _both(org_id) == (0, 0)

    async with session_maker() as db:
        a = await ledger.reserve(db, org_id, EP, 1_000)
        b = await ledger.reserve(db, org_id, EP, 2_000)
    held_a, held_b = ledger.with_margin(1_000), ledger.with_margin(2_000)
    assert await _both(org_id) == (held_a + held_b, held_a + held_b)

    # Settling below the estimate: the hold leaves, what was consumed stays.
    async with session_maker() as db:
        consumed = await ledger.settle(db, a, 400)
    assert consumed == ledger.with_margin(400)
    assert await _both(org_id) == (consumed + held_b, consumed + held_b)

    # Releasing: the hold leaves and nothing replaces it.
    async with session_maker() as db:
        await ledger.release(db, b, reason="upstream 503")
    assert await _both(org_id) == (consumed, consumed)

    # A second settle of a closed hold is a no-op for the counter, as for the balance.
    async with session_maker() as db:
        assert await ledger.settle(db, a, 999) == 0
    assert await _both(org_id) == (consumed, consumed)


async def test_settle_overrun_counts_what_was_actually_consumed(c: AsyncClient):
    org_id = await _org(c)
    async with session_maker() as db:
        call = await ledger.reserve(db, org_id, EP, 1_000)
        consumed = await ledger.settle(db, call, 5_000)  # the provider charged more than estimated
    assert consumed == ledger.with_margin(5_000)
    assert await _both(org_id) == (consumed, consumed)


async def test_counter_resets_on_a_new_utc_day(c: AsyncClient):
    org_id = await _org(c)
    async with session_maker() as db:
        call = await ledger.reserve(db, org_id, EP, 1_000)
        await ledger.settle(db, call, 1_000)
    spent = ledger.with_margin(1_000)
    assert await _both(org_id) == (spent, spent)

    # Move the counter to "yesterday" - as the day rolling over would leave it - and it reads 0.
    yesterday = date.today() - timedelta(days=1)
    async with session_maker() as db:
        await db.execute(update(Org).where(Org.id == org_id).values(spent_today_day=yesterday))
        await db.commit()
    async with session_maker() as db:
        assert await ledger.spent_today(db, org_id) == 0

    # The first movement of the new day starts from that movement, not from yesterday's total.
    async with session_maker() as db:
        await ledger.reserve(db, org_id, EP, 300)
    async with session_maker() as db:
        assert await ledger.spent_today(db, org_id) == ledger.with_margin(300)


async def test_a_hold_opened_yesterday_belongs_to_yesterday(c: AsyncClient):
    """Settling or releasing an older hold must not subtract from today what today never counted."""
    org_id = await _org(c)
    async with session_maker() as db:
        old = await ledger.reserve(db, org_id, EP, 1_000)
        await ledger.reserve(db, org_id, EP, 2_000)
    # Age the first hold: its row says yesterday, and the counter no longer includes it.
    async with session_maker() as db:
        await db.execute(update(Hold).where(Hold.id == old).values(
            created_at=ledger._now() - timedelta(days=1)))
        await db.execute(update(Org).where(Org.id == org_id).values(
            spent_today_micro=ledger.with_margin(2_000)))
        await db.commit()
    assert await _both(org_id) == (ledger.with_margin(2_000), ledger.with_margin(2_000))

    async with session_maker() as db:
        consumed = await ledger.settle(db, old, 500)  # settled TODAY, so what it consumed counts today
    expect = ledger.with_margin(2_000) + consumed
    assert await _both(org_id) == (expect, expect)

    async with session_maker() as db:
        older = await ledger.reserve(db, org_id, EP, 700)
        await db.execute(update(Hold).where(Hold.id == older).values(
            created_at=ledger._now() - timedelta(days=1)))
        await db.execute(update(Org).where(Org.id == org_id).values(spent_today_micro=expect))
        await db.commit()
    async with session_maker() as db:
        await ledger.release(db, older, reason="stale")  # nothing counted today, nothing leaves
    assert await _both(org_id) == (expect, expect)
