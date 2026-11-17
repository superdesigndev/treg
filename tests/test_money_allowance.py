"""A team's allowance: one hold that stands behind many calls, and the charges drawn against it."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.money import allowance
from treg.infra.db import reset_db, session_maker
from treg.models import CallCharge, Hold, LedgerEntry, Org

GRANT = 1_000_000
postgres_only = pytest.mark.skipif(
    not os.environ.get("TREG_TEST_DB_URL"), reason="row locks need TREG_TEST_DB_URL (Postgres)")


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


async def _open(org_id: int, amount: int, *, age: timedelta = timedelta(0)) -> str:
    """An allowance hold of `amount`, taken out of the balance, opened `age` ago."""
    async with session_maker() as db:
        db.add(Hold(id=allowance.allowance_id(org_id), org_id=org_id, amount_micro=amount,
                    allowance=True, created_at=ledger._now() - age))
        await db.execute(update(Org).where(Org.id == org_id).values(
            balance_micro=Org.balance_micro - amount,
            **(ledger._spent_today_values(amount) if not age else {})))
        await db.commit()
    return allowance.allowance_id(org_id)


async def _record(org_id: int, *costs: int, prefix: str = "call") -> list[str]:
    charges = [allowance.Charge(f"{prefix}-{index}", org_id, "e.metered", cost)
               for index, cost in enumerate(costs)]
    async with session_maker() as db:
        await allowance.record_charges(db, charges)
        await db.commit()
    return [charge.call_id for charge in charges]


async def _settles(org_id: int) -> dict[str, LedgerEntry]:
    async with session_maker() as db:
        return {entry.call_id: entry for entry in await ledger.entries_of(db, org_id, limit=500)
                if entry.kind == "settle"}


async def _daily_counter_agrees(org_id: int) -> int:
    async with session_maker() as db:
        counter = await ledger.spent_today(db, org_id)
        assert counter == await ledger.spent_today_from_ledger(db, org_id)
    return counter


async def test_a_per_call_close_and_the_stale_sweep_leave_an_allowance_hold_alone(org_id):
    hold_id = await _open(org_id, 40_000, age=timedelta(seconds=ledger.hold_ttl_s() + 5))
    async with session_maker() as db:
        assert await ledger.release(db, hold_id) == 0
        assert await ledger.settle(db, hold_id, 10) == 0
        assert await ledger.reap_stale_holds(db, org_id=org_id) == 0
    assert await _invariant(org_id) == GRANT - 40_000


async def test_recording_a_call_twice_queues_it_once(org_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_margin", 0.5)
    first = allowance.Charge("call-1", org_id, "e.one", 100, {"key_id": 7})
    async with session_maker() as db:
        assert await allowance.record_charges(db, []) == 0
        assert await allowance.record_charges(
            db, [first, allowance.Charge("call-2", org_id, "e.two", 0), first]) == 2
        await db.commit()
    async with session_maker() as db:
        assert await allowance.record_charges(
            db, [allowance.Charge("call-1", org_id, "e.one", 999),
                 allowance.Charge("call-3", org_id, "e.one", 1)]) == 1
        await db.commit()
        rows = {row.call_id: row for row in (await db.execute(select(CallCharge))).scalars()}
    assert {call_id: row.amount_micro for call_id, row in rows.items()} == {
        "call-1": 150, "call-2": 0, "call-3": 2}
    assert rows["call-1"].meta == {"key_id": 7, "observed_micro": 100, "margin": 0.5}
    assert await _invariant(org_id) == GRANT


async def test_a_fold_settles_each_call_out_of_the_allowance_hold(org_id):
    hold_id = await _open(org_id, 50_000)
    calls = await _record(org_id, 100, 0, 250)
    async with session_maker() as db:
        folded = await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
    assert folded == allowance.Folded(calls=3, charged_micro=350, consumed_micro=350)
    async with session_maker() as db:
        assert (await db.get(Hold, hold_id)).amount_micro == 49_650
        assert (await db.execute(select(CallCharge))).first() is None
        assert sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id)) == GRANT - 350
    settles = await _settles(org_id)
    assert {call_id: entry.amount_micro for call_id, entry in settles.items()} == dict(
        zip(calls, (-100, 0, -250)))
    assert len({entry.created_at for entry in settles.values()}) == 1
    first = settles[calls[0]]
    assert first.endpoint_id == "e.metered" and first.meta["allowance"] is True
    assert first.meta["observed_micro"] == first.meta["settled_micro"] == 100
    assert first.meta["consumed_micro"] == 100 and first.meta["block_shortfall_micro"] == 0
    assert await _invariant(org_id) == GRANT - 50_000
    assert await _daily_counter_agrees(org_id) == 50_000


async def test_a_fold_takes_from_the_balance_what_the_hold_does_not_cover(org_id):
    await _record(org_id, 300, prefix="unbacked")
    async with session_maker() as db:
        assert (await allowance.fold_charges_in_transaction(db, org_id)).consumed_micro == 300
        await db.commit()
    assert await _invariant(org_id) == GRANT - 300
    hold_id = await _open(org_id, 1_000)
    await _record(org_id, 700, 700, prefix="over")
    async with session_maker() as db:
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        assert (await db.get(Hold, hold_id)).amount_micro == 0
    assert await _invariant(org_id) == GRANT - 300 - 1_400
    assert await _daily_counter_agrees(org_id) == 1_700


async def test_a_call_recorded_again_after_its_fold_is_not_charged_twice(org_id):
    await _open(org_id, 10_000)
    for _ in range(2):
        await _record(org_id, 400)
        async with session_maker() as db:
            folded = await allowance.fold_charges_in_transaction(db, org_id)
            await db.commit()
    assert folded == allowance.Folded(duplicates=1)
    assert len(await _settles(org_id)) == 1
    assert await _invariant(org_id) == GRANT - 10_000
    async with session_maker() as db:
        assert sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id)) == GRANT - 400


async def test_a_fold_is_bounded_and_leaves_the_rest_queued(org_id):
    await _open(org_id, 10_000)
    await _record(org_id, 10, 10, 10)
    async with session_maker() as db:
        assert (await allowance.fold_charges_in_transaction(db, org_id, limit=2)).calls == 2
        assert (await allowance.fold_charges_in_transaction(db, org_id, limit=2)).calls == 1
        await db.commit()
    assert await _invariant(org_id) == GRANT - 10_000


async def test_a_hold_opened_yesterday_counts_its_folds_toward_today(org_id):
    await _open(org_id, 5_000, age=timedelta(days=1))
    await _record(org_id, 600)
    async with session_maker() as db:
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
    assert await _invariant(org_id) == GRANT - 5_000
    assert await _daily_counter_agrees(org_id) == 600


async def _refill(org_id: int, add: int) -> allowance.Allowance:
    async with session_maker() as db:
        result = await allowance.refill_allowance_in_transaction(db, org_id, add)
        await db.commit()
    return result


async def _entries(org_id: int, kind: str) -> list[LedgerEntry]:
    async with session_maker() as db:
        return [entry for entry in await ledger.entries_of(db, org_id, limit=500)
                if entry.kind == kind]


async def test_a_refill_opens_the_hold_then_tops_up_the_same_row(org_id):
    opened = await _refill(org_id, 30_000)
    assert opened.amount_micro == opened.added_micro == 30_000 and opened.epoch
    await _record(org_id, 1_000, 2_000)
    topped = await _refill(org_id, 20_000)
    assert topped.epoch == opened.epoch and topped.folded.calls == 2
    assert topped.amount_micro == 30_000 - 3_000 + 20_000 and topped.added_micro == 20_000
    async with session_maker() as db:
        holds = await ledger.open_holds_of(db, org_id)
    assert [(h.id, h.amount_micro, h.allowance) for h in holds] == [
        (allowance.allowance_id(org_id), 47_000, True)]
    assert holds[0].heartbeat_at is not None
    reserves = await _entries(org_id, "reserve")
    assert sorted(entry.amount_micro for entry in reserves) == [-30_000, -20_000]
    assert all(entry.call_id == holds[0].id and entry.meta["allowance"] for entry in reserves)
    assert await _invariant(org_id) == GRANT - 3_000 - 47_000
    assert await _daily_counter_agrees(org_id) == 50_000


async def test_a_refill_the_balance_cannot_cover_adds_nothing_and_still_folds(org_id):
    assert await _refill(org_id, GRANT + 1) == allowance.Allowance(None, 0, 0, allowance.Folded())
    async with session_maker() as db:
        assert await ledger.open_holds_of(db, org_id) == []
    opened = await _refill(org_id, GRANT)
    await _record(org_id, 500)
    short = await _refill(org_id, 1)
    assert short.added_micro == 0 and short.folded.calls == 1
    assert short.epoch == opened.epoch and short.amount_micro == GRANT - 500
    assert await _invariant(org_id) == 0
    assert len(await _entries(org_id, "reserve")) == 1


async def test_a_refill_of_nothing_only_folds(org_id):
    await _record(org_id, 500)
    assert await _refill(org_id, 0) == allowance.Allowance(
        None, 0, 0, allowance.Folded(calls=1, charged_micro=500, consumed_micro=500))
    assert await _invariant(org_id) == GRANT - 500


async def test_a_top_up_of_a_hold_opened_yesterday_stays_out_of_todays_counter(org_id):
    await _open(org_id, 5_000, age=timedelta(days=1))
    await _refill(org_id, 2_000)
    await _record(org_id, 300)
    await _refill(org_id, 0)
    assert await _invariant(org_id) == GRANT - 300 - 6_700
    assert await _daily_counter_agrees(org_id) == 300


async def test_closing_returns_what_is_left_and_deletes_the_hold(org_id):
    await _refill(org_id, 30_000)
    await _record(org_id, 4_000)
    async with session_maker() as db:
        closed = await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
        assert await ledger.open_holds_of(db, org_id) == []
    assert (closed.released_micro, closed.remaining_micro, closed.folded.calls) == (26_000, 0, 1)
    release, = await _entries(org_id, "release")
    assert release.amount_micro == 26_000 and release.meta["reason"] == "idle"
    assert await _invariant(org_id) == GRANT - 4_000
    assert await _daily_counter_agrees(org_id) == 4_000
    async with session_maker() as db:
        assert await allowance.close_allowance_in_transaction(db, org_id, reason="idle") == (
            allowance.Closed(0, 0, allowance.Folded()))


async def test_a_partial_close_keeps_the_hold_for_calls_still_in_flight(org_id):
    opened = await _refill(org_id, 30_000)
    async with session_maker() as db:
        closed = await allowance.close_allowance_in_transaction(
            db, org_id, reason="drain", release_micro=29_000)
        await db.commit()
    assert (closed.released_micro, closed.remaining_micro) == (29_000, 1_000)
    await _record(org_id, 800)
    again = await _refill(org_id, 10_000)
    assert again.epoch == opened.epoch and again.amount_micro == 10_200
    async with session_maker() as db:
        over = await allowance.close_allowance_in_transaction(
            db, org_id, reason="drain", release_micro=99_000)
        await db.commit()
        assert [h.amount_micro for h in await ledger.open_holds_of(db, org_id)] == [0]
    assert over.released_micro == 10_200
    assert await _invariant(org_id) == GRANT - 800
    assert await _daily_counter_agrees(org_id) == 800


async def test_closing_folds_a_backlog_larger_than_one_batch(org_id, monkeypatch):
    monkeypatch.setattr(allowance, "FOLD_BATCH", 2)
    await _refill(org_id, 10_000)
    await _record(org_id, 10, 10, 10, 10, 10)
    async with session_maker() as db:
        closed = await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
    assert closed.folded.calls == 5 and closed.released_micro == 9_950
    assert await _invariant(org_id) == GRANT - 50


@postgres_only
async def test_concurrent_refills_open_one_hold(org_id):
    results = await asyncio.gather(*(_refill(org_id, 1_000) for _ in range(8)))
    assert len({result.epoch for result in results}) == 1
    async with session_maker() as db:
        assert [h.amount_micro for h in await ledger.open_holds_of(db, org_id)] == [8_000]
    assert await _invariant(org_id) == GRANT - 8_000
    assert await _daily_counter_agrees(org_id) == 8_000


@postgres_only
async def test_folds_racing_recordings_charge_every_call_once(org_id):
    await _refill(org_id, 100_000)

    async def record(batch: int) -> None:
        await _record(org_id, *([10] * 20), prefix=f"batch-{batch}")
        await _record(org_id, *([10] * 20), prefix=f"batch-{batch}")

    async def fold() -> None:
        for _ in range(5):
            await _refill(org_id, 0)

    await asyncio.gather(*(record(batch) for batch in range(10)), *(fold() for _ in range(4)))
    await _refill(org_id, 0)
    assert len(await _settles(org_id)) == 200
    async with session_maker() as db:
        assert [h.amount_micro for h in await ledger.open_holds_of(db, org_id)] == [98_000]
    assert await _invariant(org_id) == GRANT - 100_000
    assert await _daily_counter_agrees(org_id) == 100_000
