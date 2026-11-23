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
        assert await allowance.record_charges(db, []) == set()
        assert await allowance.record_charges(
            db, [first, allowance.Charge("call-2", org_id, "e.two", 0), first,
                 allowance.Charge("call-4", org_id, "e.two", None, reserved_micro=900),
                 allowance.Charge("call-5", org_id, "e.two", 1_000, reserved_micro=600)],
        ) == {"call-1", "call-2", "call-4", "call-5"}
        await db.commit()
    async with session_maker() as db:
        assert await allowance.record_charges(
            db, [allowance.Charge("call-1", org_id, "e.one", 999),
                 allowance.Charge("call-3", org_id, "e.one", 1)]) == {"call-3"}
        await db.commit()
        rows = {row.call_id: row for row in (await db.execute(select(CallCharge))).scalars()}
    assert {call_id: row.amount_micro for call_id, row in rows.items()} == {
        "call-1": 150, "call-2": 0, "call-3": 2, "call-4": 900, "call-5": 1_500}
    assert {call_id: row.overrun_micro for call_id, row in rows.items() if row.overrun_micro} == {
        "call-5": 900}, "what a call cost beyond the 600 it set aside"
    assert rows["call-4"].meta == {"observed_micro": None, "margin": 0.5}
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


async def test_a_calls_overrun_comes_out_of_the_balance_and_leaves_the_hold_to_other_calls(org_id):
    """Two calls set aside 600 and 400 of a 1_000 hold. The first costs 1_500: the hold pays its
    600, the balance the other 900, and the 400 the second call set aside is still there."""
    hold_id = await _open(org_id, 1_000)
    async with session_maker() as db:
        await allowance.record_charges(
            db, [allowance.Charge("first", org_id, "e.metered", 1_500, reserved_micro=600)])
        await db.commit()
        folded = await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        assert (await db.get(Hold, hold_id)).amount_micro == 400
    assert folded == allowance.Folded(calls=1, charged_micro=1_500, consumed_micro=1_500)
    assert await _invariant(org_id) == GRANT - 1_500 - 400
    assert await _daily_counter_agrees(org_id) == 1_500 + 400


async def test_a_call_rewritten_after_its_fold_is_not_charged_twice(org_id):
    """A fold applies every row it claims. What keeps a folded call from being charged again
    is the rewrite, which takes its row back out of the queue."""
    await _open(org_id, 10_000)
    await _record(org_id, 400)
    async with session_maker() as db:
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
    async with session_maker() as db:
        new = await allowance.record_charges(
            db, [allowance.Charge("call-0", org_id, "e.metered", 400)])
        assert await allowance.drop_settled_charges(db, sorted(new)) == {"call-0"}
        await db.commit()
    async with session_maker() as db:
        folded = await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        settled = (await db.execute(select(LedgerEntry.amount_micro).where(
            LedgerEntry.call_id == "call-0", LedgerEntry.kind == "settle"))).scalars().all()
    assert folded == allowance.Folded() and settled == [-400]
    assert await _invariant(org_id) == GRANT - 10_000
    async with session_maker() as db:
        assert sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id)) == GRANT - 400


async def test_a_fold_is_bounded_and_leaves_the_rest_queued(org_id):
    await _open(org_id, 10_000)
    await _record(org_id, 10, 10, 10)
    for expected in (2, 1):
        async with session_maker() as db:
            assert (await allowance.fold_charges_in_transaction(db, org_id, limit=2)).calls == expected
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


async def test_a_refill_under_a_daily_cap_counts_the_fold_and_an_earlier_days_hold(org_id):
    """A hold opened yesterday is not in today's counter, yet all of it can be spent today."""
    await _open(org_id, 5_000, age=timedelta(days=1))
    await _record(org_id, 1_000)

    async def refill(add: int, cap: int) -> allowance.Allowance:
        async with session_maker() as db:
            result = await allowance.refill_allowance_in_transaction(
                db, org_id, add, daily_cap_micro=cap)
            await db.commit()
        return result

    # After the fold: 1_000 spent today, 4_000 still in yesterday's hold.
    blocked = await refill(2_000, cap=6_999)
    assert (blocked.added_micro, blocked.amount_micro, blocked.over_cap) == (0, 4_000, False)
    assert (await refill(2_000, cap=7_000)).added_micro == 2_000
    over = await refill(1, cap=6_999)
    assert (over.added_micro, over.over_cap) == (0, True)
    async with session_maker() as db:
        assert await allowance.committed_today(db, org_id) == 1_000 + 6_000
        assert await ledger.spent_today(db, org_id) == 1_000
    assert await _invariant(org_id) == GRANT - 1_000 - 6_000


async def test_a_hold_opened_today_is_already_in_the_counter_a_cap_is_checked_against(org_id):
    await _refill(org_id, 5_000)
    async with session_maker() as db:
        assert await allowance.committed_today(db, org_id) == await ledger.spent_today(db, org_id)
        result = await allowance.refill_allowance_in_transaction(
            db, org_id, 2_000, daily_cap_micro=7_000)
        assert (result.added_micro, result.over_cap) == (2_000, False)
        again = await allowance.refill_allowance_in_transaction(
            db, org_id, 1, daily_cap_micro=7_000)
        assert (again.added_micro, again.over_cap) == (0, False)
        await db.commit()


# ---- a refill sized to the team's spend --------------------------------------------------------
def _paced(spent: int, over_ms: int, *, floor: int = 1_000_000, ceiling: int = 50_000_000) -> int:
    return allowance.paced_target(spent, over_ms, window_s=30, floor_micro=floor,
                                  ceiling_micro=ceiling)


def test_a_refill_is_sized_to_what_the_team_spent_scaled_to_the_window():
    assert _paced(15_000_000, 15_000) == 30_000_000, "1 USD a second, for 30 seconds"
    assert _paced(45_000_000, 15_000) == 50_000_000, "never above the ceiling"
    assert _paced(100_000, 15_000) == 1_000_000, "a slow team keeps the chunk, as before"
    assert _paced(45_000_000, 15_000, floor=60_000_000) == 60_000_000, "nor below what a call needs"


def test_a_refill_with_no_reading_or_no_window_brings_the_counter_to_the_floor():
    assert _paced(0, 15_000) == _paced(15_000_000, 0) == 1_000_000
    assert allowance.paced_target(15_000_000, 15_000, window_s=0, floor_micro=1_000_000,
                                  ceiling_micro=50_000_000) == 1_000_000


def test_a_reading_shorter_than_half_the_window_counts_as_half_the_window():
    """A burst: 3 USD in one second would read as 90 USD over the window. Counted as half the
    window, it brings the counter up to twice the burst, not to the ceiling."""
    assert _paced(3_000_000, 1_000) == 6_000_000
    assert _paced(3_000_000, 15_000) == 6_000_000, "half the window: the same"
    assert _paced(3_000_000, 20_000) == 4_500_000, "longer than half: the pace itself"
    assert _paced(400_000, 100) == 1_000_000, "twice what was spent is below the floor"


def test_the_pace_of_a_team_that_stopped_calling_falls_back_to_the_floor():
    """The reading runs from the last refill. Nothing refills a counter nobody takes from, so
    the next refill reads what was spent before the pause over a time that includes it."""
    sizes = [_paced(15_000_000, seconds * 1000) for seconds in (15, 60, 120, 450, 3_600)]
    assert sizes == [30_000_000, 7_500_000, 3_750_000, 1_000_000, 1_000_000]


def _team(org_id: int, *, cap: int = 0):
    """`current_team` as the call runtime reads it: the cap given, the row's balance."""
    async def read(db) -> allowance.TeamNow:
        balance = (await db.execute(select(Org.balance_micro).where(Org.id == org_id))).scalar()
        return allowance.TeamNow(cap, int(balance))
    return read


async def _sized(org_id: int, add: int, grow_to: int, *, percent: int = 100, cap: int = 0,
                 shown: int | None = None, fold_limit: int = allowance.FOLD_BATCH,
                 team_cap: int = 0, ceiling: int | None = None,
                 lost_counter: bool = False) -> allowance.Allowance:
    """A sized refill. `lost_counter`: the store lost the counter, so the refill rebuilds it
    with `add` (a counter of epoch 0 names no hold)."""
    rebuild = {"counter_epoch": 0, "fresh_add_micro": add} if lost_counter else {}
    async with session_maker() as db:
        result = await allowance.refill_allowance_in_transaction(
            db, org_id, add, daily_cap_micro=cap, counter_micro=shown, fold_limit=fold_limit,
            grow_to_micro=grow_to, balance_percent=percent, ceiling_micro=ceiling,
            current_team=_team(org_id, cap=team_cap), **rebuild)
        await db.commit()
    return result


async def test_a_sized_refill_grows_the_counter_only_to_its_share_of_the_balance(org_id):
    grown = await _sized(org_id, 100_000, 900_000, percent=25)
    assert (grown.added_micro, grown.amount_micro) == (250_000, 250_000), "a quarter of the balance"
    assert await _invariant(org_id) == GRANT - 250_000
    assert [entry.amount_micro for entry in await _entries(org_id, "reserve")] == [-250_000]
    assert await _daily_counter_agrees(org_id) == 250_000
    # The hold carries 250_000 and a quarter of the 750_000 balance is less: no growth.
    assert (await _sized(org_id, 0, 900_000, percent=25, shown=200_000)).added_micro == 0
    floor = await _sized(org_id, 600_000, 900_000, percent=25, shown=0)
    assert floor.added_micro == 600_000, "what is asked for without it, whatever the share"
    assert await _invariant(org_id) == GRANT - 850_000


async def test_a_sized_refill_leaves_most_of_the_days_room_to_the_exact_path(org_id):
    """The exact path's cap check counts all of the hold as spent. The growth stops where the
    hold would carry more than a share of what the day had left, so the exact path keeps the
    rest; what is asked for without it is judged as before."""
    first = await _sized(org_id, 100_000, 900_000, percent=25, cap=800_000)
    assert first.added_micro == 200_000, "a quarter of the 800_000 the day had left"
    second = await _sized(org_id, 50_000, 900_000, percent=25, cap=800_000)
    assert second.added_micro == 50_000, "the hold already carries its share: the floor alone"
    assert (await _sized(org_id, 600_000, 900_000, cap=800_000)).added_micro == 0, (
        "no room for the floor: nothing, as before")
    assert (await _sized(org_id, 10_000, 900_000, team_cap=255_000)).added_micro == 0, (
        "the cap the team's row has now")
    async with session_maker() as db:
        assert await allowance.committed_today(db, org_id) == 250_000
    assert await _invariant(org_id) == GRANT - 250_000


async def test_a_sized_refill_counts_what_a_lost_counter_left_in_the_hold(org_id):
    """The store lost the counter: its money is still in the hold, out of every call's reach,
    and a refill rebuilds the counter beside it. The growth is measured against the whole
    hold, so the two together stay within the ceiling; past it only the floor is added."""
    await _open(org_id, 450_000)
    rebuilt = await _sized(org_id, 10_000, 300_000, ceiling=500_000, lost_counter=True)
    assert (rebuilt.added_micro, rebuilt.amount_micro) == (50_000, 500_000)
    again = await _sized(org_id, 10_000, 300_000, ceiling=500_000, lost_counter=True)
    assert (again.added_micro, again.amount_micro) == (10_000, 510_000)
    assert await _invariant(org_id) == GRANT - 510_000


async def test_growth_the_queue_already_owes_is_given_back_and_the_rest_stays(org_id):
    """A queued charge owes the balance 700_000 it still shows. The growth would leave the
    balance short of that; what is asked for without it fits, and is what the refill adds."""
    async with session_maker() as db:
        await allowance.record_charges(
            db, [allowance.Charge("over", org_id, "e.metered", 700_000, reserved_micro=0)])
        await db.commit()
    grown = await _sized(org_id, 100_000, 900_000, fold_limit=0)
    assert (grown.added_micro, grown.overdrawn) == (100_000, False)
    assert await _invariant(org_id) == GRANT - 100_000
    assert [entry.amount_micro for entry in await _entries(org_id, "reserve")] == [-100_000]
    assert await _daily_counter_agrees(org_id) == 100_000
    short = await _sized(org_id, 250_000, 900_000, fold_limit=0)
    assert (short.added_micro, short.overdrawn) == (0, False), "nor that: nothing, as before"
    assert await _invariant(org_id) == GRANT - 100_000
    assert await _daily_counter_agrees(org_id) == 100_000


async def test_a_sized_refill_reads_the_balance_it_is_measured_against(org_id):
    async with session_maker() as db:
        with pytest.raises(ValueError):
            await allowance.refill_allowance_in_transaction(
                db, org_id, 100_000, grow_to_micro=900_000)
    assert await _invariant(org_id) == GRANT


async def test_queued_charges_no_hold_stands_behind_count_toward_today(org_id):
    async with session_maker() as db:
        assert await allowance.committed_today(db, org_id + 1) == 0
        assert await allowance.committed_today(db, org_id) == 0
    await _record(org_id, 700, 300)
    async with session_maker() as db:
        assert await allowance.committed_today(db, org_id) == 1_000
    await _open(org_id, 600)  # a hold opened today covers 600 of them and is in the counter
    async with session_maker() as db:
        assert await allowance.committed_today(db, org_id) == 600 + 400
    async with session_maker() as db:
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        assert await allowance.committed_today(db, org_id) == 1_000 == (
            await ledger.spent_today(db, org_id))


async def test_a_queued_overrun_counts_toward_today_beside_what_other_calls_set_aside(org_id):
    """Two calls set aside 600 and 400 of today's hold. The first cost 1_000 and is queued; the
    second is still in flight. 1_400 is committed, before the fold and after it."""
    await _open(org_id, 1_000)
    async with session_maker() as db:
        await allowance.record_charges(
            db, [allowance.Charge("first", org_id, "e.metered", 1_000, reserved_micro=600)])
        await db.commit()
        assert await allowance.committed_today(db, org_id) == 1_400
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        assert await allowance.committed_today(db, org_id) == 1_400
    assert await _daily_counter_agrees(org_id) == 1_400


async def test_a_queued_overrun_is_not_there_for_another_reserve_to_set_aside(org_id):
    """Half the credit is in the allowance hold. One call set all of that aside and cost the
    whole credit; its charge is queued. The balance still reads half, and none of it is free."""
    await _open(org_id, GRANT // 2)
    async with session_maker() as db:
        await allowance.record_charges(
            db, [allowance.Charge("within", org_id, "e.metered", 1_000, reserved_micro=2_000)])
        await db.commit()
        await ledger.reserve(db, org_id, "e.metered", 1_000, call_id="fits")
        await ledger.release(db, "fits")
        await allowance.fold_charges_in_transaction(db, org_id)
        await allowance.record_charges(
            db, [allowance.Charge("over", org_id, "e.metered", GRANT - 1_000,
                                  reserved_micro=GRANT // 2 - 1_000)])
        await db.commit()
        assert await ledger.balance_of(db, org_id) == GRANT // 2
        with pytest.raises(ledger.InsufficientBalance) as refused:
            await ledger.reserve(db, org_id, "e.metered", 1, call_id="late")
        assert refused.value.balance_micro == 0
        await db.rollback()
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        assert await ledger.balance_of(db, org_id) == 0
    await _invariant(org_id)


async def test_a_rewrite_takes_a_settled_call_out_of_the_queue_again(org_id):
    """A rewrite queues a call whose first row was folded and deleted. Left there, it would be
    counted toward today again and be charged again by the next fold."""
    await _record(org_id, 1_000)
    async with session_maker() as db:
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
    async with session_maker() as db:
        new = await allowance.record_charges(db, [allowance.Charge("call-0", org_id, "e", 1_000),
                                                  allowance.Charge("later", org_id, "e", 300)])
        assert new == {"call-0", "later"}
        assert await allowance.drop_settled_charges(db, sorted(new)) == {"call-0"}
        await db.commit()
        assert await allowance.committed_today(db, org_id) == 1_000 + 300
        queued = (await db.execute(select(CallCharge.call_id).where(
            CallCharge.org_id == org_id))).scalars().all()
    assert queued == ["later"]


@postgres_only
async def test_a_rewrite_beside_a_fold_of_the_same_call_charges_it_once(org_id):
    """The first write's row is being folded while the rewrite runs. The rewrite's insert
    waits for the fold, queues the call again once the fold has deleted its row, and takes it
    out again: the call ends with one settle entry and no row queued."""
    from sqlalchemy import text

    await _open(org_id, 10_000)
    await _record(org_id, 400)
    async with session_maker() as fold, session_maker() as rewrite, session_maker() as watch:
        await allowance.fold_charges_in_transaction(fold, org_id)
        pid = (await rewrite.execute(text("select pg_backend_pid()"))).scalar()

        async def write_again() -> set[str]:
            new = await allowance.record_charges(
                rewrite, [allowance.Charge("call-0", org_id, "e.metered", 400)])
            new -= await allowance.drop_settled_charges(rewrite, sorted(new))
            await rewrite.commit()
            return new

        writing = asyncio.create_task(write_again())
        for _ in range(100):
            blocked = (await watch.execute(
                text("select cardinality(pg_blocking_pids(:pid))"), {"pid": pid})).scalar()
            await watch.rollback()
            if blocked:
                break
            await asyncio.sleep(0.05)
        assert blocked and not writing.done(), "the rewrite is waiting for the fold"
        await fold.commit()
        assert await asyncio.wait_for(writing, 5) == set()
    async with session_maker() as db:
        folded = await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        assert (await db.execute(select(CallCharge.call_id))).scalars().all() == []
        settled = (await db.execute(select(LedgerEntry.amount_micro).where(
            LedgerEntry.call_id == "call-0", LedgerEntry.kind == "settle"))).scalars().all()
    assert folded == allowance.Folded() and settled == [-400]


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


async def test_only_an_allowance_no_refill_has_stamped_lately_is_closed_as_stale(org_id):
    await _refill(org_id, 30_000)
    await _record(org_id, 4_000)
    now = ledger._now()
    async with session_maker() as db:
        assert await allowance.stale_allowance_orgs(db, now - timedelta(minutes=5)) == []
        assert await allowance.close_stale_allowance_in_transaction(
            db, org_id, stale_before=now - timedelta(minutes=5), reason="idle") is None
        await db.commit()
        assert (await db.execute(select(CallCharge))).first() is not None
        later = now + timedelta(minutes=5)
        assert await allowance.stale_allowance_orgs(db, later) == [org_id]
        closed = await allowance.close_stale_allowance_in_transaction(
            db, org_id, stale_before=later, reason="idle")
        await db.commit()
        assert await ledger.open_holds_of(db, org_id) == []
        assert await allowance.close_stale_allowance_in_transaction(
            db, org_id, stale_before=later, reason="idle") is None
    assert (closed.released_micro, closed.folded.calls) == (26_000, 1)
    assert await _invariant(org_id) == GRANT - 4_000


async def test_closing_folds_a_backlog_larger_than_one_batch(org_id, monkeypatch):
    monkeypatch.setattr(allowance, "FOLD_BATCH", 2)
    await _refill(org_id, 10_000)
    await _record(org_id, 10, 10, 10, 10, 10)
    async with session_maker() as db:
        closed = await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
    assert closed.folded.calls == 5 and closed.released_micro == 9_950
    assert await _invariant(org_id) == GRANT - 50


async def test_closing_a_backlog_of_several_batches_locks_the_blocks_once(org_id, monkeypatch):
    """Locking again after a batch could meet a block funded in between, out of lock order."""
    monkeypatch.setattr(allowance, "FOLD_BATCH", 2)
    locks = []

    async def lock_blocks(db, org_ids):
        locks.append(org_ids)
        return await ledger._lock_blocks(db, org_ids)

    monkeypatch.setattr(allowance, "_lock_blocks", lock_blocks)
    await _refill(org_id, 10_000)
    await _record(org_id, 10, 10, 10, 10, 10)
    await _record(org_id, 10, 10, 10, 10, 10)
    async with session_maker() as db:
        closed = await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
    assert closed.folded == allowance.Folded(calls=5, charged_micro=50, consumed_micro=50)
    assert locks == [{org_id}]


async def test_a_charge_from_an_earlier_hold_is_not_paid_out_of_the_one_opened_since(org_id):
    """A call set 600 aside from a hold that has been closed; its charge is written late. The
    hold the team has now holds 1_000 for the calls it admitted: 700 in flight, 300 on offer."""
    earlier = allowance._epoch((await _hold(org_id, await _open(org_id, 1_000))))
    async with session_maker() as db:
        await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
    await asyncio.sleep(0.002)
    current = allowance._epoch((await _hold(org_id, await _open(org_id, 1_000))))
    assert current != earlier
    async with session_maker() as db:
        await allowance.record_charges(db, [
            allowance.Charge("late", org_id, "e.metered", 600, reserved_micro=600, epoch=earlier),
            allowance.Charge("own", org_id, "e.metered", 100, reserved_micro=100, epoch=current)])
        await db.commit()
        assert await allowance.committed_today(db, org_id) == 1_000 + 600
        with pytest.raises(ledger.InsufficientBalance) as refused:
            await ledger.reserve_in_transaction(db, org_id, "e.metered", GRANT - 1_500, call_id="x")
        assert refused.value.balance_micro == GRANT - 1_000 - 600
        await db.rollback()
        folded = await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        assert folded.calls == 2
        assert [h.amount_micro for h in await ledger.open_holds_of(db, org_id)] == [900]
        assert await ledger.balance_of(db, org_id) == GRANT - 1_000 - 600
        assert await allowance.committed_today(db, org_id) == 1_000 + 600
    await _invariant(org_id)
    assert await _daily_counter_agrees(org_id) == 1_000 + 600


async def _hold(org_id: int, hold_id: str):
    async with session_maker() as db:
        return next(h for h in await ledger.open_holds_of(db, org_id) if h.id == hold_id)


async def test_a_charge_admitted_for_a_team_that_is_gone_is_dropped_not_charged(org_id):
    """A charge written late can land after its team was deleted. There is then no team to
    charge, and no ledger to write an entry in."""
    gone = org_id + 1_000
    async with session_maker() as db:  # no team with this number: nothing to write a ledger for
        await allowance.record_charges(
            db, [allowance.Charge("late", gone, "e.metered", 600, reserved_micro=600, epoch=1)])
        await db.commit()
        folded = await allowance.fold_charges_in_transaction(db, gone)
        await db.commit()
    assert folded == allowance.Folded(orphaned=1)


async def test_a_reserve_refused_for_what_the_queue_owes_leaves_the_team_as_it_was(org_id):
    await _record(org_id, GRANT)  # no hold stands behind it: all of the balance is owed
    async with session_maker() as db:
        before = await ledger.spent_today(db, org_id)
        with pytest.raises(ledger.InsufficientBalance) as refused:
            await ledger.reserve_in_transaction(db, org_id, "e.metered", 300, call_id="late")
        assert (refused.value.balance_micro, refused.value.required_micro) == (0, 300)
        with pytest.raises(ledger.InsufficientBalance) as refused:
            await ledger.reserve_in_transaction(db, org_id, "e.metered", GRANT * 2, call_id="more")
        assert refused.value.balance_micro == 0, "both refusals report what is free of the queue"
        await db.commit()
        assert await ledger.balance_of(db, org_id) == GRANT
        assert await ledger.spent_today(db, org_id) == before
        assert await ledger.open_holds_of(db, org_id) == []
    await _invariant(org_id)


async def test_a_reserve_of_nothing_is_refused_before_a_fold_as_it_is_after(org_id):
    """A call estimated at zero is still charged what it turns out to cost. A team whose queue
    owes more than its balance cannot pay for it, whether or not the queue has been applied."""
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "e.metered", 600, call_id="in-flight")
    await _record(org_id, GRANT - 100)
    async with session_maker() as db:
        with pytest.raises(ledger.InsufficientBalance) as refused:
            await ledger.reserve_in_transaction(db, org_id, "e.metered", 0, call_id="zero")
        assert refused.value.balance_micro == -500
        await db.rollback()
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
        with pytest.raises(ledger.InsufficientBalance) as refused:
            await ledger.reserve_in_transaction(db, org_id, "e.metered", 0, call_id="zero")
        assert refused.value.balance_micro == -500


@postgres_only
async def test_a_reserve_does_not_spend_a_refund_that_a_queued_overrun_already_owes(org_id):
    """300 is free. A reserve of 300 waits for a hold's release on the team's row; meanwhile an
    overrun of 1_100 is queued. When the reserve gets the row the balance reads 1_300 and 200
    of it is free: the queue it decides on must be as new as the balance."""
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "e.metered", GRANT - 1_300, call_id="rest")
        await ledger.reserve(db, org_id, "e.metered", 1_000, call_id="held")

    async def reserve() -> None:
        async with session_maker() as db:
            await ledger.reserve(db, org_id, "e.metered", 300, call_id="waited")

    async with session_maker() as db:
        await ledger.release_in_transaction(db, "held")
        await db.flush()
        waiting = asyncio.create_task(reserve())
        await asyncio.sleep(0.3)
        assert not waiting.done(), "waits for the release's lock on the team"
        async with session_maker() as other:
            await allowance.record_charges(
                other, [allowance.Charge("over", org_id, "e.metered", 1_100, reserved_micro=0)])
            await other.commit()
        await db.commit()
    with pytest.raises(ledger.InsufficientBalance) as refused:
        await waiting
    assert refused.value.balance_micro == 200
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 1_300


@postgres_only
async def test_a_reserve_that_waited_for_a_fold_counts_its_charges_once(org_id):
    """A queued overrun leaves 400 free of a balance of 1_000, before the fold and after it. A
    reserve of 300 that waits for the fold's lock on the team must not subtract the overrun
    from the balance the fold has already taken it from."""
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "e.metered", GRANT - 1_000, call_id="rest")
        await allowance.record_charges(
            db, [allowance.Charge("over", org_id, "e.metered", 600, reserved_micro=0)])
        await db.commit()

    async def reserve() -> None:
        async with session_maker() as db:
            await ledger.reserve(db, org_id, "e.metered", 300, call_id="waited")

    async with session_maker() as db:
        await allowance.fold_charges_in_transaction(db, org_id)
        await db.flush()
        waiting = asyncio.create_task(reserve())
        await asyncio.sleep(0.3)
        assert not waiting.done(), "waits for the fold's lock on the team"
        await db.commit()
    await waiting
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 100


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
        # The second write is a rewrite of the first: the only way a call is recorded twice.
        ids = await _record(org_id, *([10] * 20), prefix=f"batch-{batch}")
        async with session_maker() as db:
            new = await allowance.record_charges(
                db, [allowance.Charge(call_id, org_id, "e.metered", 10) for call_id in ids])
            await allowance.drop_settled_charges(db, sorted(new))
            await db.commit()

    async def fold() -> None:
        for _ in range(5):
            await _refill(org_id, 0)

    await asyncio.gather(*(record(batch) for batch in range(10)), *(fold() for _ in range(4)))
    await _refill(org_id, 0)
    async with session_maker() as db:
        settled = (await db.execute(select(LedgerEntry.call_id).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle"))).scalars().all()
    assert len(settled) == len(set(settled)) == 200
    async with session_maker() as db:
        assert [h.amount_micro for h in await ledger.open_holds_of(db, org_id)] == [98_000]
    assert await _invariant(org_id) == GRANT - 100_000
    assert await _daily_counter_agrees(org_id) == 100_000
