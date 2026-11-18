"""The call path's use of a team's allowance: take, refill, finish, and the ways back to the exact path."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from treg.application import allowance_sweep
from treg.application.call import allowance as runtime
from treg.application.call import reserve as call_reserve
from treg.application.call import settle as call_settle
from treg.application.call.intake import CallMeta
from treg.application.call.resolve import MarketplaceCall
from treg.application.call.types import ReservationFailed
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.money import allowance
from treg.domain.governance.teams import cascade_delete_org, lock_allowance_holds
from treg.infra import db as infra_db
from treg.infra import kv
from treg.infra.db import reset_db, session_maker
from treg.domain.identity.access import Caller
from treg.models import ArchiveKeyOrg, CallCharge, Hold, LedgerEntry, Membership, Org, Tool, User

GRANT = 5_000_000
CHUNK = 1_000_000
_AHEAD = runtime._ahead


class FakeStore:
    """The allowance counter and the refill lease, in memory, with the real store's answers."""

    def __init__(self) -> None:
        self.counters: dict[int, dict] = {}
        self.leases: dict[str, str] = {}
        self.until_ms = 0
        self.lease_answer: str | None = None

    def expire(self) -> None:
        """Move the clock past the deadline: takes stop until an add sets a later one."""
        self.until_ms = 0

    def _frozen(self, counter) -> bool:
        return counter.get("frozen", 0) != counter.get("thawed", 0)

    async def allowance_take(self, org_id, amount, *, ahead_ms=0, low_micro=0):
        await asyncio.sleep(0)  # a real store call lets other calls run meanwhile
        counter = self.counters.get(org_id)
        now = time.time() * 1000
        if (counter is None or self._frozen(counter) or now > self.until_ms
                or counter.get("avail", 0) < amount):
            return 0
        counter["avail"] -= amount
        near = now + ahead_ms > self.until_ms
        if (near or counter["avail"] < low_micro) and (
                now - counter.get("asked", 0) >= 2000
                or (near and counter.get("asked_for") != self.until_ms)):
            counter["asked"], counter["asked_for"] = now, self.until_ms
            return -counter["epoch"]
        return counter["epoch"]

    async def allowance_take_up_to(self, org_id, epoch, amount):
        counter = self.counters.get(org_id)
        if counter is None or counter.get("epoch") != epoch:
            return 0
        taken = min(counter["avail"], amount)
        counter["avail"] -= taken
        return taken

    async def allowance_give_back(self, org_id, epoch, amount):
        counter = self.counters.get(org_id)
        if counter is None or counter.get("epoch") != epoch:
            return False
        counter["avail"] += amount
        return True

    async def allowance_add(self, org_id, epoch, amount, *, until_ms, ttl_ms, seen_frozen):
        counter = self.counters.get(org_id)
        if counter is not None and counter.get("epoch", 0) > epoch:
            return "stale"
        if counter is None or counter.get("epoch") != epoch:
            counter = self.counters[org_id] = {
                "epoch": epoch, "avail": 0, "frozen": (counter or {}).get("frozen", 0),
                "thawed": (counter or {}).get("thawed", 0)}
        counter["avail"] += amount
        if amount > 0:
            counter.pop("asked", None)
        if counter.setdefault("thawed", 0) < seen_frozen <= counter["frozen"]:
            counter["thawed"] = seen_frozen
        self.until_ms = max(self.until_ms, until_ms)
        return counter["avail"]

    async def allowance_state(self, org_id):
        counter = self.counters.get(org_id, {})
        return (counter.get("avail", 0), counter.get("frozen", 0), counter.get("thawed", 0),
                counter.get("epoch", 0))

    async def allowance_drop(self, org_id, epoch):
        counter = self.counters.get(org_id)
        if counter is None or "avail" not in counter or counter.get("epoch") != epoch:
            return 0
        left, counter["avail"] = counter["avail"], 0
        counter["frozen"] = counter.get("frozen", 0) + 1
        return left

    async def allowance_freeze(self, org_id, *, ttl_ms=0):
        counter = self.counters.setdefault(org_id, {})
        counter["frozen"] = counter.get("frozen", 0) + 1
        return counter["frozen"]

    async def allowance_retire(self, org_id, epoch, ttl_ms):
        counter = self.counters.get(org_id)
        if counter is not None and counter.get("epoch", 0) > epoch:
            return 0
        left = counter["avail"] if counter is not None and counter.get("epoch") == epoch else 0
        self.counters[org_id] = {"epoch": epoch + 1, "avail": 0,
                                 "frozen": (counter or {}).get("frozen", 0),
                                 "thawed": (counter or {}).get("thawed", 0)}
        return left

    async def acquire_lease(self, key, token, ttl_ms):
        if self.lease_answer is not None:
            return self.lease_answer
        if key in self.leases:
            return "busy"
        self.leases[key] = token
        return "acquired"

    async def release_lease(self, key, token):
        if self.leases.get(key) == token:
            del self.leases[key]
        return "released"


@pytest.fixture
async def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(kv, "store", lambda: fake)
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(runtime, "_refused_until", {})
    monkeypatch.setattr(runtime, "_charges", runtime._ChargeWriter())
    monkeypatch.setattr(runtime, "_in_flight", [0])
    # The allowance is PostgreSQL only; its logic is still tested on whatever the suite runs on.
    monkeypatch.setattr(runtime, "_database_supported", lambda: True)
    monkeypatch.setattr(runtime, "_freezes_due", {})
    monkeypatch.setattr(runtime, "_freeze_waiters", {})
    monkeypatch.setattr(runtime, "_freeze_tasks", {})
    monkeypatch.setattr(runtime, "_freeze_wake", {})
    monkeypatch.setattr(runtime, "_FREEZE_RETRY_S", 0.001)
    # No refill ahead of the deadline unless a test asks for one: each asserts exact amounts.
    monkeypatch.setattr(runtime, "_ahead", lambda: {"ahead_ms": 0, "low_micro": 0})
    monkeypatch.setattr(runtime, "_refreshing", {})
    return fake


@pytest.fixture
async def org_id(monkeypatch):
    await reset_db()
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_margin", 0)
    monkeypatch.setattr(settings, "money_allowance_chunk_micro", CHUNK)
    monkeypatch.setattr(settings, "money_allowance_linger_ms", 1)
    async with session_maker() as db:
        org = Org(id=int(uuid4().hex[:7], 16) + 1, name="allowance", slug=uuid4().hex)
        db.add(org)
        await db.flush()
        await ledger.grant(db, org.id, amount_micro=GRANT)
        await db.commit()
        monkeypatch.setattr(settings, "money_allowance_org_ids", [org.id])
        return org.id


async def _state(org_id: int) -> tuple[int, int]:
    """(balance, allowance hold amount), with the invariant checked."""
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        blocks = sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id))
        holds = await ledger.open_holds_of(db, org_id)
    assert balance == blocks - sum(h.amount_micro for h in holds)
    return balance, sum(h.amount_micro for h in holds if h.allowance)


async def _queued(org_id: int) -> int:
    async with session_maker() as db:
        return len((await db.execute(
            select(CallCharge.call_id).where(CallCharge.org_id == org_id))).all())


async def _finish(org_id, call_id, epoch, reserved, **kwargs) -> int:
    return await runtime.finish(org_id, call_id, epoch, reserved, endpoint_id="e.metered", **kwargs)


async def _fold(org_id: int) -> allowance.Folded:
    async with session_maker() as db:
        folded = await allowance.fold_charges_in_transaction(db, org_id)
        await db.commit()
    return folded


async def test_the_first_take_opens_the_allowance_and_later_ones_touch_no_balance(org_id, store):
    assert runtime.enabled_for(org_id) and not runtime.enabled_for(org_id + 1)
    epoch = await runtime.take(org_id, 600)
    assert epoch and store.counters[org_id]["avail"] == CHUNK - 600
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)
    assert await runtime.take(org_id, 600) == epoch
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)


async def test_finishing_a_call_charges_what_it_cost_and_gives_back_the_rest(org_id, store):
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=450,
                         meta={"provider": "p"}) == 450
    assert store.counters[org_id]["avail"] == CHUNK - 450
    async with session_maker() as db:
        row = (await db.execute(select(CallCharge))).scalar_one()
    assert (row.call_id, row.amount_micro, row.meta["provider"]) == ("call-1", 450, "p")
    assert (await _fold(org_id)).consumed_micro == 450
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK - 450)


async def test_a_call_that_is_not_billable_gives_everything_back_and_writes_nothing(org_id, store):
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-1", epoch, 600, billable=False) == 0
    assert store.counters[org_id]["avail"] == CHUNK
    async with session_maker() as db:
        assert (await db.execute(select(CallCharge))).first() is None


async def _occupy_the_rest_of_the_balance(org_id: int) -> None:
    """Leave the team nothing but its allowance, as when other holds have the rest."""
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "e.other", await ledger.balance_of(db, org_id),
                             call_id="other-hold")


async def test_an_overrun_is_owed_by_the_balance_and_stops_the_counter_until_a_refill_looks(
        org_id, store, monkeypatch):
    """Two calls set aside 600 and 400 of a 1_000 hold; the team has nothing else. The first
    costs 1_500. When the second is cancelled, its 400 must not be offered to a new call: the
    team is 500 overdrawn."""
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 1_000)
    first = await runtime.take(org_id, 600)
    second = await runtime.take(org_id, 400)
    await _occupy_the_rest_of_the_balance(org_id)
    assert await _finish(org_id, "first", first, 600, billable=True, actual_micro=1_500) == 1_500
    assert await _state(org_id) == (-900, 400), "the hold still has what the second set aside"
    assert await _finish(org_id, "second", second, 400, billable=False) == 0
    assert store.counters[org_id]["avail"] == 400
    assert await runtime.take(org_id, 400) is None, "frozen: only a refill may reopen it"
    assert store.counters[org_id]["avail"] == 0
    assert await _state(org_id) == (-500, 0), "what was left went back; the exact path decides"


async def test_an_overrun_on_a_team_with_money_reopens_at_the_next_refill(org_id, store):
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-1", epoch, 600, billable=True) == 600
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-2", epoch, 600, billable=True, actual_micro=900) == 900
    assert store.counters[org_id]["avail"] == CHUNK - 1_200
    assert await _state(org_id) == (GRANT - CHUNK - 300, CHUNK - 1_200)
    assert await runtime.take(org_id, 600) == epoch
    assert store.counters[org_id]["avail"] == CHUNK - 600
    assert await _state(org_id) == (GRANT - CHUNK - 300 - 1_200, CHUNK)


async def test_a_call_is_told_what_its_teams_credit_really_paid(org_id, store, monkeypatch):
    """All the team has is set aside; the call costs twice that. The ledger can take only what
    there is and records the rest as a shortfall, and the call is told the same figure."""
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", GRANT)
    epoch = await runtime.take(org_id, GRANT)
    assert await _finish(org_id, "call-1", epoch, GRANT, billable=True,
                         actual_micro=2 * GRANT) == GRANT
    async with session_maker() as db:
        entry, = [e for e in await ledger.entries_of(db, org_id) if e.kind == "settle"]
    assert entry.amount_micro == -GRANT and entry.meta["block_shortfall_micro"] == GRANT


async def test_a_counter_left_above_its_hold_is_brought_back_down_by_the_next_refill(
        org_id, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 1_000)
    epoch = await runtime.take(org_id, 100)
    store.counters[org_id]["avail"] += 5_000  # as if a give-back was applied twice
    store.expire()
    assert await runtime.take(org_id, 100) == epoch
    balance, hold = await _state(org_id)
    assert store.counters[org_id]["avail"] == hold - 100


async def test_calls_finishing_together_share_one_insert(org_id, store, monkeypatch):
    writes = []
    write = runtime._ChargeWriter._write

    async def counted(batch, **kwargs):
        writes.append(len(batch))
        return await write(batch, **kwargs)

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(counted))
    epochs = [await runtime.take(org_id, 100) for _ in range(40)]
    charged = await asyncio.gather(*(
        _finish(org_id, f"call-{index}", epoch, 100, billable=True, actual_micro=70)
        for index, epoch in enumerate(epochs)))
    assert charged == [70] * 40 and writes == [40]
    assert (await _fold(org_id)).calls == 40


async def _instantly(monkeypatch) -> None:
    real_sleep = asyncio.sleep

    async def soon(delay):
        await real_sleep(0)

    monkeypatch.setattr(runtime.asyncio, "sleep", soon)


async def test_a_write_that_committed_but_reported_failure_is_charged_and_not_given_back(
        org_id, store, monkeypatch):
    """The first attempt commits and its answer is lost; the retry finds the database down.
    Two errors do not mean nothing was written."""
    write, attempts = runtime._ChargeWriter._write, []

    async def lossy(rows, **kwargs):
        attempts.append(len(rows))
        if len(attempts) == 1:
            await write(rows)
        if len(attempts) <= 2:
            raise ConnectionError("lost")
        return await write(rows, **kwargs)

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(lossy))
    epoch = await runtime.take(org_id, 600)
    await _instantly(monkeypatch)
    assert await _finish(org_id, "call-1", epoch, 600, billable=True) == 600
    assert store.counters[org_id]["avail"] == CHUNK - 600, "what was charged is not offered again"
    await runtime._charges._retry_task
    assert runtime._charges._unconfirmed == [] and len(attempts) == 3
    assert (await _fold(org_id)) == allowance.Folded(calls=1, charged_micro=600, consumed_micro=600)
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK - 600)


async def test_a_charge_the_database_refused_is_written_once_the_database_is_back(
        org_id, store, monkeypatch):
    write, attempts = runtime._ChargeWriter._write, []

    async def down_then_up(rows, **kwargs):
        attempts.append(len(rows))
        if len(attempts) <= 3:
            raise ConnectionError("database down")
        return await write(rows, **kwargs)

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(down_then_up))
    epoch = await runtime.take(org_id, 600)
    await _instantly(monkeypatch)
    assert await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=450) == 450
    assert store.counters[org_id]["avail"] == CHUNK - 450
    async with session_maker() as db:
        assert (await db.execute(select(CallCharge))).first() is None
    await runtime._charges._retry_task
    assert (await _fold(org_id)).consumed_micro == 450


async def test_no_call_is_admitted_while_charges_wait_to_be_written(org_id, store, monkeypatch):
    """With the database refusing writes, each admitted call would add a charge to a bounded
    retry queue. So none is admitted until the queue is written."""
    write, down = runtime._ChargeWriter._write, [True]

    async def maybe(rows, **kwargs):
        if down[0]:
            raise ConnectionError("database down")
        return await write(rows, **kwargs)

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(maybe))
    first, second = await runtime.take(org_id, 100), await runtime.take(org_id, 100)
    real_sleep = asyncio.sleep

    async def retry_waits(delay):
        await real_sleep(0 if delay < 1 else 0.02)

    monkeypatch.setattr(runtime.asyncio, "sleep", retry_waits)
    assert await _finish(org_id, "a", first, 100, billable=True) == 100
    assert await runtime.take(org_id, 100) is None, "this process is behind on its charges"
    assert await _finish(org_id, "b", second, 100, billable=True) == 100
    down[0] = False
    await runtime._charges._retry_task
    assert await _queued(org_id) == 2, "both charges landed once the database was back"
    assert await runtime.take(org_id, 100) == first


async def test_a_retried_batch_marks_only_the_paid_questions_its_rows_are_new_for(
        org_id, store, monkeypatch):
    """One charge of the batch was committed by the lost first attempt, the other was not. The
    rewrite must still mark the second's question as paid."""
    marked = []

    async def note(db, uses):
        marked.extend(uses)

    monkeypatch.setattr(runtime.archive, "note_org_uses_in_transaction", note)
    async with session_maker() as db:
        await allowance.record_charges(db, [allowance.Charge("a", org_id, "e", 100)])
        await db.commit()
    await runtime._ChargeWriter._write([
        (allowance.Charge("a", org_id, "e", 100), (org_id, "question-a")),
        (allowance.Charge("b", org_id, "e", 100), (org_id, "question-b")),
    ])
    assert marked == [(org_id, "question-b")] and await _queued(org_id) == 2


async def test_takes_racing_an_empty_counter_refill_it_once(org_id, store):
    epochs = await asyncio.gather(*(runtime.take(org_id, 1_000) for _ in range(30)))
    assert len(set(epochs)) == 1 and epochs[0]
    assert store.counters[org_id]["avail"] == CHUNK - 30_000
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)


async def test_a_refill_tops_the_counter_back_up_and_folds_what_was_queued(org_id, store):
    epoch = await runtime.take(org_id, CHUNK)
    await _finish(org_id, "call-1", epoch, CHUNK, billable=True, actual_micro=CHUNK - 100)
    assert store.counters[org_id]["avail"] == 100
    assert await runtime.take(org_id, 500) == epoch
    assert store.counters[org_id]["avail"] == CHUNK - 500
    assert await _state(org_id) == (GRANT - 2 * CHUNK + 100, CHUNK)
    async with session_maker() as db:
        assert (await db.execute(select(CallCharge))).first() is None


@pytest.mark.parametrize("left", [CHUNK, 600, 0])
async def test_a_counter_left_by_a_closed_hold_does_not_count_toward_the_next_one(
        org_id, store, left):
    """The sweep closed the hold and the store did not take the counter's retiring. Whatever
    the counter still shows belongs to no hold: the next hold is opened for a whole chunk, and
    its counter replaces the old one."""
    epoch = await runtime.take(org_id, CHUNK)
    await store.allowance_give_back(org_id, epoch, left)
    async with session_maker() as db:
        await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
    store.expire()
    assert store.counters[org_id]["avail"] == left
    rebuilt = await runtime.take(org_id, 500)
    assert rebuilt and rebuilt > epoch
    assert store.counters[org_id]["avail"] == CHUNK - 500
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)


async def test_a_drain_that_arrives_late_takes_nothing_from_a_later_hold(org_id, store, monkeypatch):
    """A drain empties the counter, then stalls. The hold is closed and another opened, with a
    call admitted against it. What the old counter held is not the new hold's to return."""
    old = await runtime.take(org_id, 100)
    await runtime.finish(org_id, "first", old, 100, endpoint_id="e", billable=False)
    drop = store.allowance_drop

    async def stalls_after(org, epoch):
        left = await drop(org, epoch)
        monkeypatch.setattr(store, "allowance_drop", drop)
        async with session_maker() as db:
            await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
            await db.commit()
        await store.allowance_retire(org, epoch, 60_000)
        assert await runtime.take(org_id, 700), "a later hold, with a call in flight"
        return left

    monkeypatch.setattr(store, "allowance_drop", stalls_after)
    await runtime._drain(org_id, old, "late")
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK), "the later hold is whole"
    assert store.counters[org_id]["avail"] == CHUNK - 700
    await runtime._drain(org_id, old, "late again")
    assert store.counters[org_id]["avail"] == CHUNK - 700, "nor is the later counter emptied"


async def test_a_charge_its_hold_no_longer_backs_stops_the_counter_like_an_overrun(
        org_id, store, monkeypatch):
    """A call set 600 aside from a hold that was closed before its charge was written. The
    charge is within what it set aside, and all of it comes out of the balance, which has
    nothing. The 300 the later hold's counter still offers must not be handed out."""
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 1_000)
    old = await runtime.take(org_id, 600)
    async with session_maker() as db:
        await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
    await store.allowance_retire(org_id, old, 60_000)
    later = await runtime.take(org_id, 700)
    assert later and later != old
    await _occupy_the_rest_of_the_balance(org_id)
    assert await _state(org_id) == (0, 1_000) and store.counters[org_id]["avail"] == 300
    assert await _finish(org_id, "late", old, 600, billable=True, actual_micro=600) == 600
    counter = store.counters[org_id]
    assert counter["frozen"] != counter["thawed"], "frozen though nothing overran"
    assert await runtime.take(org_id, 100) is None, "a refill read the balance: it is below zero"
    assert store.counters[org_id]["avail"] == 0
    assert await _state(org_id) == (-300, 700), "the later hold keeps what its call set aside"


async def _statements(run) -> list[str]:
    seen: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.lower().split()))

    engine = infra_db._engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await run()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return seen


async def test_the_charge_writer_decides_a_charges_hold_inside_a_real_transaction(org_id, store):
    """PostgreSQL: the shared lock on the hold row, then the insert. SQLite takes no row lock
    and begins its transaction at the first write, so there the insert comes first and the
    hold is read under the lock it took."""
    epoch = await runtime.take(org_id, 600)
    charge = allowance.Charge("ordered", org_id, "e.metered", 400, None, 600, epoch)
    seen = await _statements(lambda: runtime._ChargeWriter._write([(charge, None)]))
    insert = next(i for i, text in enumerate(seen) if text.startswith("insert into callcharge"))
    hold = next(i for i, text in enumerate(seen) if " from hold " in text)
    if infra_db._is_sqlite:
        assert insert < hold
    else:
        assert hold < insert and seen[hold].endswith("for share")
        assert any("idle_in_transaction_session_timeout" in text for text in seen[:hold])


async def test_deleting_a_team_locks_its_allowance_hold_before_its_queued_charges(org_id, store):
    """The order the charge writer uses. The other way round, a writer holding the hold while it
    writes a charge again and a deletion that has deleted that charge wait on each other."""
    epoch = await runtime.take(org_id, 600)
    await _finish(org_id, "queued", epoch, 600, billable=True, actual_micro=400)

    async def delete() -> None:
        async with session_maker() as db:
            await cascade_delete_org(await db.get(Org, org_id), db)
            await db.rollback()

    seen = await _statements(delete)
    lock = next(i for i, text in enumerate(seen) if "from hold where hold.id in" in text)
    charge = next(i for i, text in enumerate(seen) if "delete from callcharge" in text)
    assert lock < charge
    assert infra_db._is_sqlite or seen[lock].endswith("for update")


async def test_teams_deleted_together_have_their_holds_locked_in_the_writers_order(org_id, store):
    """One statement for the batch, ordered by the holds' ids as the writer's is: team by team
    in any other order, two deletions and a writer's batch could each hold what the other
    wants."""
    seen = await _statements(lambda: _lock_holds([org_id + 7, org_id, org_id + 3]))
    assert len(seen) == 1 and "order by hold.id" in seen[0]


async def _lock_holds(org_ids) -> None:
    async with session_maker() as db:
        await lock_allowance_holds(db, org_ids)
        await db.rollback()


async def test_a_process_about_to_exit_wakes_a_freeze_that_is_pausing(org_id, store, monkeypatch):
    """The pause before the next attempt can be longer than the wait at exit. The store is
    back; the attempt must be made now."""
    monkeypatch.setattr(runtime, "_FREEZE_RETRY_S", 30.0)
    freeze, away = store.allowance_freeze, [True]

    async def sometimes_away(org, **kwargs):
        return None if away[0] else await freeze(org, **kwargs)

    monkeypatch.setattr(store, "allowance_freeze", sometimes_away)
    assert await runtime.freeze(org_id) is False
    away[0] = False
    assert await asyncio.wait_for(runtime.deliver_owed_freezes(timeout_s=2), 3) == 0
    assert store.counters[org_id]["frozen"] == 1


async def test_deleting_a_team_retires_its_counter(org_id, store):
    """The counter admits calls by the team's number alone. With the team gone it must admit
    none, for a new team that is given the number as well."""
    epoch = await runtime.take(org_id, 100)
    assert store.counters[org_id]["avail"] == CHUNK - 100
    async with session_maker() as db:
        await cascade_delete_org(await db.get(Org, org_id), db)
        await db.commit()
    assert (store.counters[org_id]["avail"], store.counters[org_id]["epoch"]) == (0, epoch + 1)
    assert await store.allowance_take(org_id, 1) == 0


async def test_the_writer_drops_a_charge_and_its_paid_mark_when_the_team_is_gone(org_id, store):
    """Nothing to queue, and no paid-question mark for a later team of the same number."""
    gone = org_id + 1_000
    charge = allowance.Charge("late", gone, "e.metered", 400, None, 600, 1)
    await runtime._ChargeWriter._write([(charge, (gone, "question"))])
    async with session_maker() as db:
        assert (await db.execute(select(CallCharge).where(CallCharge.org_id == gone))).first() is None
        assert (await db.execute(
            select(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == gone))).first() is None


async def test_a_teams_freeze_does_not_wait_for_another_teams_write(org_id, store, monkeypatch):
    """One team's overrun is committed; the next team's write then fails. The first team's
    counter is still admitting calls, so its freeze cannot wait for the batch."""
    epoch = await runtime.take(org_id, 600)
    later_team = org_id + 1
    write_team = runtime._ChargeWriter._write_team

    async def second_fails(team, rows, again):
        if team == later_team:
            raise ConnectionError("database away")
        return await write_team(team, rows, again)

    monkeypatch.setattr(runtime._ChargeWriter, "_write_team", staticmethod(second_fails))
    rows = [(allowance.Charge("over", org_id, "e.metered", 900, None, 600, epoch), None),
            (allowance.Charge("other", later_team, "e.metered", 100, None, 100, None), None)]
    with pytest.raises(ConnectionError):
        await runtime._ChargeWriter._write(rows)
    counter = store.counters[org_id]
    assert counter["frozen"] != counter["thawed"] and await _queued(org_id) == 1


async def test_the_allowance_is_off_where_the_database_is_not_postgresql(org_id, store, monkeypatch):
    assert runtime.enabled_for(org_id)
    monkeypatch.setattr(runtime, "_database_supported", lambda: False)
    assert not runtime.enabled_for(org_id)


async def test_a_hold_whose_counter_was_retired_keeps_what_calls_in_flight_stand_on(
        org_id, store, monkeypatch):
    """A deletion of the team retired the counter and then did not commit. The store will take
    nothing more for that hold, so nothing is added to it; and a call admitted before still
    stands on its money, so nothing is returned until the sweep knows it idle."""
    old = await runtime.take(org_id, 700)
    async with session_maker() as db:
        stamped = next(h for h in await ledger.open_holds_of(db, org_id) if h.allowance).heartbeat_at
    await store.allowance_retire(org_id, old, 60_000)
    assert await runtime.take(org_id, 100) is None
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK), "nothing added, nothing returned"
    async with session_maker() as db:
        hold = next(h for h in await ledger.open_holds_of(db, org_id) if h.allowance)
        assert hold.heartbeat_at == stamped, "its deadline is not moved on"
    assert await _finish(org_id, "in-flight", old, 700, billable=True, actual_micro=700) == 700
    await _fold(org_id)
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK - 700), "paid from the hold it took from"
    async with session_maker() as db:  # the idle sweep, once no call can still stand on it
        await allowance.close_allowance_in_transaction(db, org_id, reason="idle")
        await db.commit()
    monkeypatch.setattr(runtime, "_refused_until", {})
    new = await runtime.take(org_id, 100)
    assert new and new > old + 1
    assert await _state(org_id) == (GRANT - 700 - CHUNK, CHUNK)


async def test_a_freeze_the_store_refuses_one_team_does_not_keep_another_teams_charge_unwritten(
        org_id, store, monkeypatch):
    """The first team's overrun is committed and its freeze not taken. The second team's charge
    has nothing to do with that: it is written, not left in this process's memory."""
    earlier_team, epoch = org_id - 1, await runtime.take(org_id, 100)
    async with session_maker() as db:
        db.add(Org(id=earlier_team, name="earlier", slug=uuid4().hex))
        await db.commit()

    async def refuses(org, **kwargs):
        return None

    monkeypatch.setattr(store, "allowance_freeze", refuses)
    rows = [(allowance.Charge("over", earlier_team, "e.metered", 900, None, 600, None), None),
            (allowance.Charge("plain", org_id, "e.metered", 100, None, 100, epoch), None)]
    with pytest.raises(ConnectionError):
        await runtime._ChargeWriter._write(rows)
    assert await _queued(earlier_team) == 1 and await _queued(org_id) == 1


async def test_the_writer_holds_one_teams_hold_at_a_time(org_id, store):
    """A batch for two teams is two transactions, so no order between their holds exists for a
    deletion of several teams to contradict."""
    other = org_id + 1
    async with session_maker() as db:
        db.add(Org(id=other, name="other", slug=uuid4().hex))
        await db.commit()
    rows = [(allowance.Charge(f"c-{team}", team, "e.metered", 400, None, 600, None), None)
            for team in (other, org_id)]
    seen = await _statements(lambda: runtime._ChargeWriter._write(rows))
    inserts = [i for i, text in enumerate(seen) if text.startswith("insert into callcharge")]
    holds = [i for i, text in enumerate(seen) if " from hold " in text]
    assert len(inserts) == 2 and len(holds) == 2, "each team's hold read with its own insert"
    assert await _queued(org_id) == 1 and await _queued(other) == 1


async def test_a_charge_carries_the_hold_its_call_took_from(org_id, store):
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call", epoch, 600, billable=True, actual_micro=400) == 400
    async with session_maker() as db:
        named = (await db.execute(select(CallCharge.hold_created_at).where(
            CallCharge.call_id == "call"))).scalar_one()
        hold = next(h for h in await ledger.open_holds_of(db, org_id) if h.allowance)
    assert named == hold.created_at and allowance._epoch(hold) == epoch


async def test_a_balance_that_cannot_refill_drains_the_allowance_for_the_exact_path(
        org_id, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", GRANT)
    epoch = await runtime.take(org_id, GRANT - 1_000)
    await _finish(org_id, "call-1", epoch, GRANT - 1_000, billable=True, actual_micro=GRANT - 1_000)
    assert store.counters[org_id]["avail"] == 1_000
    assert await runtime.take(org_id, 5_000) is None
    assert store.counters[org_id]["avail"] == 0
    assert await _state(org_id) == (1_000, 0)
    assert await runtime.take(org_id, 500) is None  # cooling down: no refill per call
    monkeypatch.setattr(runtime, "_refused_until", {})
    assert await runtime.take(org_id, 500) is None  # less than a chunk left: still the exact path
    assert await _state(org_id) == (1_000, 0)


async def test_a_team_with_no_balance_opens_nothing(org_id, store):
    assert await runtime.take(org_id, GRANT + 1) is None
    assert await _state(org_id) == (GRANT, 0)
    assert store.counters == {}


async def test_a_daily_cap_with_no_room_for_a_refill_sends_the_call_to_the_exact_path(org_id, store):
    assert await runtime.take(org_id, 600, daily_cap_micro=CHUNK - 1) is None
    assert await _state(org_id) == (GRANT, 0)


async def test_a_counter_no_longer_fresh_is_refreshed_and_topped_up_by_a_refill(org_id, store):
    epoch = await runtime.take(org_id, 600)
    store.expire()
    assert await runtime.take(org_id, 600) == epoch
    assert store.counters[org_id]["avail"] == CHUNK - 600
    assert await _state(org_id) == (GRANT - CHUNK - 600, CHUNK + 600)


async def test_a_store_that_cannot_answer_sends_the_call_to_the_exact_path(org_id, monkeypatch):
    monkeypatch.setattr(kv, "store", lambda: kv.LocalStore())
    monkeypatch.setattr(runtime, "_refused_until", {})
    assert await runtime.take(org_id, 600) is None
    assert await _state(org_id) == (GRANT, 0)


async def test_a_refill_that_fails_sends_the_call_to_the_exact_path(org_id, store, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(allowance, "refill_allowance_in_transaction", broken)
    assert await runtime.take(org_id, 600) is None
    assert store.leases == {}


# ---- through the call path's own reserve, settle and cancel ------------------------------------
async def _caller(org_id: int) -> Caller:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        user = User(email=f"{uuid4().hex}@example.invalid")
        db.add(user)
        await db.flush()
        membership = Membership(user_id=user.id, org_id=org_id, role="owner", token_hash=uuid4().hex)
        db.add(membership)
        await db.commit()
        for row in (org, user, membership):
            await db.refresh(row)
            db.expunge(row)
        return Caller(membership, user, org, None)


def _call(org_id: int, estimate: int = 600, **fields) -> MarketplaceCall:
    return MarketplaceCall(
        tool=Tool(org_id=org_id, name="test", owner="test@example.invalid",
                  base_url="https://example.invalid", host="example.invalid"),
        upstream="https://example.invalid/call", consumed=set(), endpoint_id="test.allowance",
        provider="test", tier="platform", cost_type="per_call", estimate_micro=estimate,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": estimate}, **fields)


async def _per_call_holds(org_id: int) -> list[str]:
    async with session_maker() as db:
        return [h.id for h in await ledger.open_holds_of(db, org_id) if not h.allowance]


async def test_an_eligible_call_reserves_and_settles_without_a_hold_of_its_own(org_id, store):
    caller, mk = await _caller(org_id), _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="direct-1", allow_allowance=True)
    assert mk.call_id == "direct-1" and mk.allowance_epoch and mk.reserved_micro == 600
    assert await _per_call_holds(org_id) == []
    finalized = []
    assert await call_settle._platform_settle(
        mk, 200, observed_override=450, finalized=lambda: finalized.append(1)) == (450, 450)
    assert mk.call_id is None and mk.allowance_epoch is None and finalized == [1]
    assert store.counters[org_id]["avail"] == CHUNK - 450
    assert await call_settle._platform_settle(mk, 200, observed_override=450) == (0, None)
    async with session_maker() as db:
        row = (await db.execute(select(CallCharge))).scalar_one()
    assert (row.call_id, row.amount_micro, row.endpoint_id) == ("direct-1", 450, "test.allowance")
    assert row.meta["provider"] == "test" and row.meta["status_code"] == 200
    assert (await _fold(org_id)).consumed_micro == 450
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK - 450)


async def test_an_eligible_call_the_provider_never_answered_returns_its_take(org_id, store):
    caller, mk = await _caller(org_id), _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="direct-1", allow_allowance=True)
    assert await call_settle._platform_settle(mk, None, reason="network") == (0, None)
    assert store.counters[org_id]["avail"] == CHUNK
    async with session_maker() as db:
        assert (await db.execute(select(CallCharge))).first() is None


async def test_a_cancelled_eligible_call_returns_its_take(org_id, store):
    caller, mk = await _caller(org_id), _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="direct-1", allow_allowance=True)
    await call_settle._finish_cancelled_call(None, mk, "direct-1")
    assert mk.allowance_epoch is None and store.counters[org_id]["avail"] == CHUNK
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)


@pytest.mark.parametrize("why", [
    "not_allowed_by_the_caller", "team_not_listed", "tagged", "child_call", "billed_oauth",
    "routed_child", "store_unavailable",
])
async def test_every_other_call_keeps_its_own_hold(org_id, store, monkeypatch, why):
    caller, mk, meta, call_ref, allow = await _caller(org_id), _call(org_id), CallMeta(tags={}), "c-1", True
    if why == "not_allowed_by_the_caller":
        allow = False
    elif why == "team_not_listed":
        monkeypatch.setattr(get_settings(), "money_allowance_org_ids", [])
    elif why == "tagged":
        meta = CallMeta(tags={"user": "u1"})
    elif why == "child_call":
        call_ref = "c-1:r1"
    elif why == "billed_oauth":
        mk = _call(org_id, billed_oauth=True)
    elif why == "routed_child":
        mk = _call(org_id, deferred=[])
    elif why == "store_unavailable":
        monkeypatch.setattr(kv, "store", lambda: kv.LocalStore())
    await call_reserve._platform_reserve(mk, caller, meta, call_ref, allow_allowance=allow)
    assert mk.allowance_epoch is None and mk.call_id == call_ref
    assert await _per_call_holds(org_id) == [call_ref]
    assert store.counters == {}


async def test_a_member_with_a_spend_limit_keeps_a_hold_of_their_own(org_id, store):
    caller = await _caller(org_id)
    caller.membership.role = "member"
    caller.membership.daily_spend_cap_micro = 5_000
    mk = _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="c-1", allow_allowance=True)
    assert mk.allowance_epoch is None and await _per_call_holds(org_id) == ["c-1"]


async def test_a_team_too_poor_for_a_chunk_is_served_by_the_exact_path(org_id, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", GRANT + 1)
    caller, mk = await _caller(org_id), _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="c-1", allow_allowance=True)
    assert mk.allowance_epoch is None and await _per_call_holds(org_id) == ["c-1"]
    assert await call_settle._platform_settle(mk, 200, observed_override=450) == (450, 450)
    assert await _state(org_id) == (GRANT - 450, 0)


# ---- the idle sweep ----------------------------------------------------------------------------
async def _age_heartbeat(org_id: int, seconds: float) -> None:
    async with session_maker() as db:
        await db.execute(update(Hold).where(Hold.org_id == org_id, Hold.allowance.is_(True)).values(
            heartbeat_at=ledger._now() - timedelta(seconds=seconds)))
        await db.commit()


async def test_the_sweep_returns_an_idle_allowance_with_what_a_lost_take_left_in_it(org_id, store):
    epoch = await runtime.take(org_id, 600)
    await runtime.take(org_id, 700)  # a call that crashed: never finished
    await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=450)
    assert await allowance_sweep.sweep_once(session_maker) == 0, "refilled a moment ago"
    await _age_heartbeat(org_id, allowance_sweep.idle_after_s() - 5)
    assert await allowance_sweep.sweep_once(session_maker) == 0
    await _age_heartbeat(org_id, allowance_sweep.idle_after_s() + 5)
    assert await allowance_sweep.sweep_once(session_maker) == 1
    assert await _state(org_id) == (GRANT - 450, 0)
    assert (store.counters[org_id]["epoch"], store.counters[org_id]["avail"]) == (epoch + 1, 0)
    assert await allowance_sweep.sweep_once(session_maker) == 0
    new_epoch = await runtime.take(org_id, 600)
    assert new_epoch > epoch and await _state(org_id) == (GRANT - 450 - CHUNK, CHUNK)


async def test_the_sweep_trusts_the_deadline_on_the_hold_not_its_own_settings(
        org_id, store, monkeypatch):
    """A refill published a long window; a process with a shorter one configured then sweeps.
    Calls are still being admitted, so the hold must stay."""
    monkeypatch.setattr(get_settings(), "money_allowance_fresh_s", 3_600)
    await runtime.take(org_id, 100)
    monkeypatch.setattr(get_settings(), "money_allowance_fresh_s", 1)
    now = ledger._now()
    monkeypatch.setattr(allowance_sweep, "utcnow_naive", lambda: now + timedelta(seconds=1_800))
    assert await allowance_sweep.sweep_once(session_maker) == 0
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)
    await runtime.take(org_id, 100)  # this refill has the shorter window; the hold keeps the later
    assert await allowance_sweep.sweep_once(session_maker) == 0
    monkeypatch.setattr(allowance_sweep, "utcnow_naive", lambda: now + timedelta(
        seconds=3_600 + allowance_sweep.idle_after_s() + 5))
    assert await allowance_sweep.sweep_once(session_maker) == 1


@pytest.mark.parametrize("row_locks", [False, True])
async def test_without_a_lease_the_sweep_writes_money_only_where_rows_lock(
        org_id, store, monkeypatch, row_locks):
    """The store cannot answer, so no process can take the team's lease. PostgreSQL still makes
    two sweeps wait on the hold's row; SQLite does not, and there the write is put off."""
    epoch = await runtime.take(org_id, 600)
    await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=450)
    async with session_maker() as db:
        await db.execute(update(CallCharge).values(created_at=ledger._now() - timedelta(hours=1)))
        await db.commit()
    await _age_heartbeat(org_id, allowance_sweep.idle_after_s() + 5)
    store.lease_answer = "unavailable"
    monkeypatch.setattr(allowance_sweep, "_row_locks", lambda: row_locks)
    assert await allowance_sweep.fold_queued_once(session_maker) == (1 if row_locks else 0)
    assert await allowance_sweep.sweep_once(session_maker) == (1 if row_locks else 0)
    assert await _state(org_id) == ((GRANT - 450, 0) if row_locks else (GRANT - CHUNK, CHUNK))


async def test_one_team_failing_to_close_does_not_stop_the_sweep(org_id, store, monkeypatch):
    await runtime.take(org_id, 600)
    await _age_heartbeat(org_id, allowance_sweep.idle_after_s() + 5)

    async def broken(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(allowance, "close_stale_allowance_in_transaction", broken)
    assert await allowance_sweep.sweep_once(session_maker) == 0
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)


async def test_a_refill_published_late_admits_no_call_past_what_its_stamp_covers(
        org_id, store, monkeypatch):
    """The process stalls between the refill's commit and its publish. The sweep judges the
    hold by the stamp, so the publish must not open a fresh window from the moment it lands."""
    add = store.allowance_add

    async def late(org, epoch, amount, *, until_ms, **kwargs):
        fresh_ms = get_settings().money_allowance_fresh_s * 1000
        return await add(org, epoch, amount, until_ms=until_ms - fresh_ms - 1, **kwargs)

    monkeypatch.setattr(store, "allowance_add", late)  # as if published a whole window late
    assert await runtime.take(org_id, 600) is None
    assert store.counters[org_id]["avail"] == CHUNK, "the money is there; no call may take it"
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)


# ---- who may let calls spend again after an overrun ---------------------------------------------
async def test_a_refill_that_committed_before_an_overrun_cannot_lift_its_freeze(
        org_id, store, monkeypatch):
    """A refill commits, and before it publishes, a call ends with an overrun and freezes the
    counter. The refill read the balance without that charge, so its publish may not thaw."""
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 1_000)
    in_flight = await runtime.take(org_id, 600)
    await _occupy_the_rest_of_the_balance(org_id)
    store.expire()
    add = store.allowance_add

    told, published = [], []

    async def overrun_lands_first(*args, **kwargs):
        monkeypatch.setattr(store, "allowance_add", add)
        told.append(await _finish(org_id, "over", in_flight, 600, billable=True,
                                  actual_micro=1_500))
        published.append(await add(*args, **kwargs))
        return published[-1]

    monkeypatch.setattr(store, "allowance_add", overrun_lands_first)
    monkeypatch.setattr(runtime, "_SETTLED_TRIES", 1)  # the refill in progress holds the lease
    assert await runtime.take(org_id, 100) is None, "published, but still frozen"
    assert told == [1_500] and published == [400], "the refill did publish"
    counter = store.counters[org_id]
    assert (counter["frozen"], counter["thawed"]) == (1, 0), "and thawed nothing it had not read"
    assert await runtime.take(org_id, 100) is None, "the next refill sees the team overdrawn"
    balance, hold = await _state(org_id)
    assert balance < 0 and hold == 0 and store.counters[org_id]["avail"] == 0


async def test_a_frozen_counter_is_reopened_only_after_the_whole_queue_is_applied(
        org_id, store, monkeypatch):
    """More is queued than one batch, and the charge that froze the counter is in a later
    batch. A refill that applied one batch would read a balance without it."""
    monkeypatch.setattr(allowance, "FOLD_BATCH", 2)
    monkeypatch.setattr(runtime, "_fold_in_background", lambda org_id: None)
    async with session_maker() as db:
        await allowance.refill_allowance_in_transaction(db, org_id, 1_000)
        await allowance.record_charges(db, [
            allowance.Charge(f"queued-{index}", org_id, "e", 10, reserved_micro=10)
            for index in range(5)] + [
            allowance.Charge("zz-overrun", org_id, "e", GRANT * 2, reserved_micro=10)])
        await db.commit()
    assert await runtime.take(org_id, 100) is None  # no counter yet: this refill only opens it
    store.counters.setdefault(org_id, {"epoch": 1, "avail": 0})
    await store.allowance_freeze(org_id)
    monkeypatch.setattr(runtime, "_refused_until", {})
    assert await runtime.take(org_id, 100) is None
    assert await _queued(org_id) == 0, "every queued charge was applied before deciding"
    balance, _ = await _state(org_id)
    assert balance < 0


async def test_a_call_finishing_beside_an_overrun_is_told_what_the_ledger_took(
        org_id, store, monkeypatch):
    """Two calls set aside 3M and 2M of a team's 5M. The first costs all 5M. The second costs
    exactly what it set aside, but there is no credit left for it: its settle entry is 0."""
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", GRANT)
    first = await runtime.take(org_id, 3_000_000)
    second = await runtime.take(org_id, 2_000_000)
    assert await _finish(org_id, "first", first, 3_000_000, billable=True,
                         actual_micro=GRANT) == GRANT
    assert await _finish(org_id, "second", second, 2_000_000, billable=True) == 0
    async with session_maker() as db:
        entries = {e.call_id: e for e in await ledger.entries_of(db, org_id) if e.kind == "settle"}
    assert entries["second"].amount_micro == 0
    assert entries["second"].meta["block_shortfall_micro"] == 2_000_000


async def test_a_call_waits_for_another_process_to_apply_its_charge(org_id, store, monkeypatch):
    """The team's lease is held elsewhere, so this process cannot fold. Whoever holds it is
    folding; the call waits for its settle entry instead of answering with the charge."""
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", GRANT)
    first = await runtime.take(org_id, 3_000_000)
    second = await runtime.take(org_id, 2_000_000)
    await _finish(org_id, "first", first, 3_000_000, billable=True, actual_micro=GRANT)
    store.leases[runtime.money_key(org_id)] = "another-process"
    real_sleep, waits = asyncio.sleep, []

    async def other_process_folds(delay):
        waits.append(delay)
        if len(waits) == 3:
            await _fold(org_id)
        await real_sleep(0)

    monkeypatch.setattr(runtime.asyncio, "sleep", other_process_folds)
    assert await _finish(org_id, "second", second, 2_000_000, billable=True) == 0


async def test_an_overrun_written_late_freezes_the_counter_when_it_lands(org_id, store, monkeypatch):
    """The charge was not in the database when its call ended, so a refill since then could not
    have applied it. Landing must freeze again."""
    write, down = runtime._ChargeWriter._write, [True]

    async def maybe(rows, **kwargs):
        if down[0]:
            raise ConnectionError("database down")
        return await write(rows, **kwargs)

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(maybe))
    epoch = await runtime.take(org_id, 600)
    real_sleep = asyncio.sleep

    async def retry_waits(delay):
        await real_sleep(0 if delay < 1 else 0.02)

    monkeypatch.setattr(runtime.asyncio, "sleep", retry_waits)
    assert await _finish(org_id, "over", epoch, 600, billable=True, actual_micro=900) == 900
    counter = store.counters[org_id]
    assert counter.get("frozen", 0) == 0, "not frozen for a charge no refill could apply yet"
    down[0] = False
    await runtime._charges._retry_task
    assert counter["frozen"] == 1 and await _queued(org_id) == 1


async def test_a_call_cancelled_while_its_charge_is_written_still_freezes_for_its_overrun(
        org_id, store, monkeypatch):
    """The request goes away after its charge is queued. The writer commits the charge, and the
    freeze must not depend on the request still being there."""
    monkeypatch.setattr(get_settings(), "money_allowance_linger_ms", 30)
    epoch = await runtime.take(org_id, 600)
    request = asyncio.create_task(
        _finish(org_id, "over", epoch, 600, billable=True, actual_micro=900))
    await asyncio.sleep(0.005)
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    await runtime._charges._task
    counter = store.counters[org_id]
    assert await _queued(org_id) == 1 and counter["frozen"] == 1
    assert await runtime.take(org_id, 100) == epoch, "a refill after the commit applies it"
    assert await _queued(org_id) == 0 and counter["thawed"] == 1


async def test_a_refill_between_an_overruns_end_and_its_commit_does_not_leave_it_ungated(
        org_id, store, monkeypatch):
    """The call has ended; its charge is not in the database yet. A refill now cannot see the
    charge and must not be what decides. The freeze comes with the commit, after it."""
    monkeypatch.setattr(get_settings(), "money_allowance_linger_ms", 30)
    epoch = await runtime.take(org_id, 600)
    request = asyncio.create_task(
        _finish(org_id, "over", epoch, 600, billable=True, actual_micro=900))
    await asyncio.sleep(0.005)
    store.expire()
    assert await runtime.take(org_id, 100) == epoch, "nothing is known yet: an ordinary refill"
    assert await request == 900
    counter = store.counters[org_id]
    assert counter["frozen"] == 1 and counter["thawed"] == 0, "frozen by the commit, not thawed"
    assert await runtime.take(org_id, 100) == epoch
    assert counter["thawed"] == 1 and await _queued(org_id) == 0


async def test_a_committed_overrun_whose_freeze_does_not_reach_the_store_is_gone_through_again(
        org_id, store, monkeypatch):
    freeze, calls = store.allowance_freeze, []

    async def flaky(org, **kwargs):
        calls.append(org)
        return None if len(calls) <= 2 else await freeze(org, **kwargs)

    monkeypatch.setattr(store, "allowance_freeze", flaky)
    epoch = await runtime.take(org_id, 600)
    await _instantly(monkeypatch)
    assert await _finish(org_id, "over", epoch, 600, billable=True, actual_micro=900) == 900
    assert runtime._charges.behind(), "the write and its one retry both lacked the freeze"
    await runtime._charges._retry_task
    assert store.counters[org_id]["frozen"] == 1 and not runtime._charges.behind()
    assert runtime._charges.owed == 0 and len(calls) == 3


async def test_a_settlement_outside_the_allowance_that_overran_freezes_the_counter(
        org_id, store, monkeypatch):
    """A tagged call keeps its own hold. It costs more than the hold and may have used the
    team's last credit; calls admitted from the counter would not meet the balance gate."""
    caller = await _caller(org_id)
    epoch = await runtime.take(org_id, 100)
    for call_ref, cost, frozen in (("tagged-ok", 450, 0), ("tagged-over", 900, 1)):
        mk = _call(org_id)
        await call_reserve._platform_reserve(mk, caller, CallMeta(tags={"user": "u"}), call_ref)
        assert mk.allowance_epoch is None
        assert await call_settle._platform_settle(mk, 200, observed_override=cost) == (cost, cost)
        assert store.counters[org_id].get("frozen", 0) == frozen
    assert await runtime.take(org_id, 100) == epoch, "a refill read the balance and reopened it"
    # The hold decides, not this process's list: another process may still list the team.
    monkeypatch.setattr(get_settings(), "money_allowance_org_ids", [])
    mk = _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="unlisted-over")
    await call_settle._platform_settle(mk, 200, observed_override=900)
    assert store.counters[org_id]["frozen"] == 2
    async with session_maker() as db:
        await allowance.close_allowance_in_transaction(db, org_id, reason="test")
        await db.commit()
    mk = _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="no-hold-over")
    await call_settle._platform_settle(mk, 200, observed_override=900)
    assert store.counters[org_id]["frozen"] == 2, "a team with no allowance hold has nothing to freeze"


async def test_a_freeze_the_store_did_not_take_is_sent_until_it_has(org_id, store, monkeypatch):
    """The settlement is committed and cannot be undone for a store that is away. The freeze is
    owed: this process stops taking for the team, and keeps sending."""
    caller = await _caller(org_id)
    epoch = await runtime.take(org_id, 100)
    monkeypatch.setattr(runtime, "_freezes_due", {})
    freeze, away, calls = store.allowance_freeze, [True], []

    async def sometimes_away(org, **kwargs):
        calls.append(org)
        if away[0] and len(calls) % 2:
            return None
        if away[0]:
            raise ConnectionError("store away")
        return await freeze(org, **kwargs)

    monkeypatch.setattr(store, "allowance_freeze", sometimes_away)
    await _instantly(monkeypatch)
    mk = _call(org_id)
    await call_reserve._platform_reserve(mk, caller, CallMeta(tags={"user": "u"}), "tagged-over")
    assert await call_settle._platform_settle(mk, 200, observed_override=900) == (900, 900)
    assert runtime._freezes_due == {org_id: 1} and store.counters[org_id].get("frozen", 0) == 0
    assert await runtime.take(org_id, 100) is None, "this process owes the team a freeze"
    while len(calls) < 4:
        await asyncio.sleep(0.001)
    assert runtime._freezes_due == {org_id: 1}, "still sending"
    away[0] = False
    await runtime._freeze_tasks[org_id]
    assert not runtime._freezes_due and store.counters[org_id]["frozen"] == 1
    assert await runtime.take(org_id, 100) == epoch


async def test_a_freeze_is_handed_over_before_the_request_can_be_cancelled(org_id, store):
    """Asking is not a coroutine: nothing the request awaits afterwards has to finish."""
    await runtime.take(org_id, 100)
    assert runtime.freeze(org_id) is not None
    assert await runtime.take(org_id, 100) is None, "owed from the moment it is asked"
    await runtime._freeze_tasks[org_id]
    assert store.counters[org_id]["frozen"] == 1 and not runtime._freezes_due


async def test_a_freeze_asked_while_another_is_on_its_way_is_sent_as_well(org_id, store, monkeypatch):
    """The one on its way may be read, and thawed, by a refill that ran before the second
    settlement committed. Its arrival says nothing about the second."""
    await runtime.take(org_id, 100)
    freeze, on_its_way, arrive = store.allowance_freeze, asyncio.Event(), asyncio.Event()

    async def slow_once(org, **kwargs):
        if not on_its_way.is_set():
            on_its_way.set()
            await arrive.wait()
        return await freeze(org, **kwargs)

    monkeypatch.setattr(store, "allowance_freeze", slow_once)
    first = runtime.freeze(org_id)
    await on_its_way.wait()
    second = runtime.freeze(org_id)
    arrive.set()
    assert await first is True and await second is True
    assert store.counters[org_id]["frozen"] == 2 and not runtime._freezes_due


async def test_a_team_the_store_refuses_does_not_hold_up_anothers_freeze_or_its_own_next(
        org_id, store, monkeypatch):
    """Settlements wait for their freeze to be tried once. That wait must not include the pause
    before another team's next attempt, nor the pause before this team's own."""
    monkeypatch.setattr(runtime, "_FREEZE_RETRY_S", 30.0)
    other, freeze, refused = org_id + 1, store.allowance_freeze, [True]

    async def refuses_one_team(org, **kwargs):
        return None if org == org_id and refused[0] else await freeze(org, **kwargs)

    monkeypatch.setattr(store, "allowance_freeze", refuses_one_team)
    assert await asyncio.wait_for(runtime.freeze(org_id), 1) is False
    assert await asyncio.wait_for(runtime.freeze(other), 1) is True
    assert await asyncio.wait_for(runtime.freeze(org_id), 1) is False, "tried at once, not after the pause"
    refused[0] = False
    assert await asyncio.wait_for(runtime.freeze(org_id), 1) is True
    await asyncio.sleep(0)
    assert not runtime._freezes_due and store.counters[org_id]["frozen"] == 1
    assert not runtime._freeze_tasks and not runtime._freeze_wake


async def test_a_process_about_to_exit_waits_for_the_freezes_it_owes(org_id, store, monkeypatch):
    """A worker tick ends its process. A freeze still being retried would go with it."""
    freeze, away = store.allowance_freeze, [True]

    async def sometimes_away(org, **kwargs):
        return None if away[0] else await freeze(org, **kwargs)

    monkeypatch.setattr(store, "allowance_freeze", sometimes_away)
    assert await runtime.freeze(org_id) is False
    assert await runtime.deliver_owed_freezes(timeout_s=0.02) == 1, "still owed: reported, not hidden"
    away[0] = False
    assert await runtime.deliver_owed_freezes(timeout_s=5) == 0
    assert store.counters[org_id]["frozen"] == 1


async def test_an_overrun_outside_the_allowance_before_the_first_publish_holds_the_new_counter(
        org_id, store, monkeypatch):
    """The first refill has committed its hold and not published. A tagged call overruns now:
    there is no counter to freeze, and the one the refill builds read the balance before it."""
    caller = await _caller(org_id)
    add = store.allowance_add
    mk = _call(org_id)
    await call_reserve._platform_reserve(mk, caller, CallMeta(tags={"user": "u"}), "tagged-over")

    async def overrun_lands_first(*args, **kwargs):
        monkeypatch.setattr(store, "allowance_add", add)
        await call_settle._platform_settle(mk, 200, observed_override=900)
        return await add(*args, **kwargs)

    monkeypatch.setattr(store, "allowance_add", overrun_lands_first)
    assert await runtime.take(org_id, 100) is None, "built frozen"
    counter = store.counters[org_id]
    assert (counter["avail"] > 0, counter["frozen"], counter["thawed"]) == (True, 1, 0)
    assert await runtime.take(org_id, 100), "the next refill read the balance after the overrun"


async def test_a_settle_entry_that_cannot_be_read_back_is_not_answered_as_free(
        org_id, store, monkeypatch):
    async def broken(db, org, call_id):
        raise ConnectionError("database down")

    epoch = await runtime.take(org_id, 600)
    monkeypatch.setattr(allowance, "settled_micro", broken)
    assert await _finish(org_id, "over", epoch, 600, billable=True, actual_micro=900) == 900


async def test_a_counter_lost_during_a_refill_is_not_left_frozen_for_good(org_id, store):
    """The refill read three freezes, then the store lost the counter. The rebuilt one has none,
    and thawing it to three would leave the two counts unequal with nothing to raise the other."""
    epoch = await runtime.take(org_id, 600)
    for _ in range(3):
        await store.allowance_freeze(org_id)
    add = store.allowance_add

    async def lost_first(org, *args, **kwargs):
        store.counters.pop(org)
        return await add(org, *args, **kwargs)

    store.allowance_add = lost_first
    try:
        assert await runtime.take(org_id, 100) == epoch
    finally:
        store.allowance_add = add
    assert await runtime.take(org_id, 100) == epoch


# ---- the retry queue's bound -------------------------------------------------------------------
async def test_calls_are_admitted_only_while_there_is_a_place_for_their_charge(
        org_id, store, monkeypatch):
    """Calls arriving together, and charges being written, all count: a place is taken before
    the first await and handed to the writer until its row is known written."""
    monkeypatch.setattr(runtime, "_MAX_OWED", 3)
    monkeypatch.setattr(runtime, "_in_flight", [0])
    await runtime.take(org_id, 100)  # opens the allowance, so the next takes are one step each
    taken = await asyncio.gather(*(runtime.take(org_id, 100) for _ in range(6)))
    assert sum(1 for epoch in taken if epoch) == 2 and runtime._in_flight == [3]
    epoch = next(epoch for epoch in taken if epoch)
    await _finish(org_id, "a", epoch, 100, billable=False)
    assert runtime._in_flight == [2] and await runtime.take(org_id, 100) == epoch

    release = asyncio.Event()
    write = runtime._ChargeWriter._write

    async def slow(rows, **kwargs):
        await release.wait()
        return await write(rows, **kwargs)

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(slow))
    charging = asyncio.create_task(_finish(org_id, "b", epoch, 100, billable=True))
    await asyncio.sleep(0.02)  # the batch has left the pending list and is being written
    assert (runtime._in_flight, runtime._charges.owed) == ([2], 1)
    assert await runtime.take(org_id, 100) is None, "the row being written still holds its place"
    release.set()
    assert await charging == 100
    assert runtime._charges.owed == 0 and await runtime.take(org_id, 100) == epoch


async def test_a_take_is_on_the_call_before_the_auto_top_up_read_can_be_cancelled(
        org_id, store, monkeypatch):
    caller, mk = await _caller(org_id), _call(org_id)
    caller.org.autotopup_enabled, caller.org.autotopup_consented_at = True, ledger._now()
    reading = asyncio.Event()

    async def never_returns(org):
        reading.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(call_reserve, "_schedule_autotopup_after", never_returns)
    reserve = asyncio.create_task(
        call_reserve._platform_reserve(mk, caller, call_ref="c-1", allow_allowance=True))
    await reading.wait()
    reserve.cancel()
    await asyncio.gather(reserve, return_exceptions=True)
    assert mk.allowance_epoch and mk.call_id == "c-1"
    await call_settle._finish_cancelled_call(None, mk, "c-1")
    assert store.counters[org_id]["avail"] == CHUNK and runtime._in_flight[0] == 0


async def test_a_charge_rewritten_after_it_was_applied_does_not_mark_its_question_again(
        org_id, store, monkeypatch):
    """The first attempt committed, the charge was folded and its queue row deleted. To the
    rewrite the row looks new, but the first attempt already counted the paid question."""
    marked = []

    async def note(db, uses):
        marked.extend(uses)

    monkeypatch.setattr(runtime.archive, "note_org_uses_in_transaction", note)
    rows = [(allowance.Charge("a", org_id, "e", 100), (org_id, "question-a"))]
    await runtime._ChargeWriter._write(rows)
    await _fold(org_id)
    await runtime._ChargeWriter._write(rows, again=True)
    assert marked == [(org_id, "question-a")]
    assert (await _fold(org_id)).duplicates == 1


# ---- keeping up with the queue -----------------------------------------------------------------
async def test_a_busy_teams_queue_does_not_outgrow_what_a_refill_folds(org_id, store, monkeypatch):
    """A chunk pays for more calls than one refill folds. The queue must still come back down."""
    monkeypatch.setattr(runtime, "_REFILL_FOLD", 5)
    monkeypatch.setattr(runtime, "_BACKLOG_FOLD", 5)
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 2_000)
    monkeypatch.setattr(runtime, "_folding", {})
    backlog = []
    for round_ in range(4):
        for index in range(20):  # twenty calls of 100 spend one chunk
            epoch = await runtime.take(org_id, 100)
            await _finish(org_id, f"r{round_}-{index}", epoch, 100, billable=True)
        epoch = await runtime.take(org_id, 100)  # the counter is empty: this one refills
        await _finish(org_id, f"r{round_}-refill", epoch, 100, billable=False)
        if runtime._folding.get(org_id):
            await runtime._folding[org_id]
        backlog.append(await _queued(org_id))
    assert max(backlog) < 5, backlog
    assert store.leases == {}
    balance, hold = await _state(org_id)
    assert store.counters[org_id]["avail"] <= hold


async def test_cheap_calls_that_never_empty_the_counter_are_still_folded(org_id, store, monkeypatch):
    """One chunk pays for all of these, so no refill comes to fold them."""
    monkeypatch.setattr(runtime, "_REFILL_FOLD", 5)
    monkeypatch.setattr(runtime, "_BACKLOG_FOLD", 5)
    monkeypatch.setattr(runtime, "_folding", {})
    backlog = []
    for index in range(23):
        epoch = await runtime.take(org_id, 10)
        await _finish(org_id, f"cheap-{index}", epoch, 10, billable=True)
        if runtime._folding.get(org_id):
            await runtime._folding[org_id]
        backlog.append(await _queued(org_id))
    assert runtime.refills(org_id) == 1 and max(backlog) <= 5 and backlog[-1] == 3, backlog
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK - 200)


async def test_the_sweep_folds_a_queue_nothing_else_is_folding(org_id, store, monkeypatch):
    epoch = await runtime.take(org_id, 600)
    await _finish(org_id, "old-1", epoch, 600, billable=True, actual_micro=450)
    assert await allowance_sweep.fold_queued_once(session_maker) == 0, "queued a moment ago"
    async with session_maker() as db:
        await db.execute(update(CallCharge).values(
            created_at=ledger._now() - timedelta(seconds=allowance_sweep._SWEEP_EVERY_S + 5)))
        await db.commit()
    monkeypatch.setattr(get_settings(), "money_allowance_org_ids", [])  # no longer listed
    assert await allowance_sweep.fold_queued_once(session_maker) == 1
    assert await _queued(org_id) == 0
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK - 450)


# ---- one money session per team, across processes ---------------------------------------------
async def test_no_allowance_money_session_starts_while_another_process_holds_the_teams_lease(
        org_id, store, monkeypatch):
    """Refill, fold, close: each reads the hold and then writes it. Without row locks (SQLite)
    two processes doing that at once lose a write, so they all take one lease."""
    monkeypatch.setattr(runtime, "_REFILL_WAIT_S", 0.02)
    epoch = await runtime.take(org_id, 600)
    await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=450)
    async with session_maker() as db:
        await db.execute(update(CallCharge).values(created_at=ledger._now() - timedelta(hours=1)))
        await db.execute(update(Hold).where(Hold.allowance.is_(True)).values(
            heartbeat_at=ledger._now() - timedelta(hours=1)))
        await db.commit()
    store.leases[runtime.money_key(org_id)] = "another-process"
    store.expire()
    await runtime._fold_backlog(org_id)
    assert await allowance_sweep.fold_queued_once(session_maker) == 0
    assert await allowance_sweep.sweep_once(session_maker) == 0
    assert await runtime.take(org_id, 600) is None
    assert await _queued(org_id) == 1 and await _state(org_id) == (GRANT - CHUNK, CHUNK)
    del store.leases[runtime.money_key(org_id)]
    assert await allowance_sweep.fold_queued_once(session_maker) == 1
    assert await allowance_sweep.sweep_once(session_maker) == 1
    assert store.leases == {}


# ---- a daily cap across midnight ---------------------------------------------------------------
async def _move_hold_to_yesterday(org_id: int) -> None:
    """The hold's birth names it; its counter and the charges queued against it carry the same
    name, and all move. A take made before this carries the old name: take again after."""
    born = ledger._now() - timedelta(days=1)
    kv.store().counters[org_id]["epoch"] = (born - datetime(1970, 1, 1)) // timedelta(microseconds=1)
    async with session_maker() as db:
        await db.execute(update(Hold).where(Hold.org_id == org_id, Hold.allowance.is_(True)).values(
            created_at=born))
        await db.execute(update(CallCharge).where(CallCharge.org_id == org_id).values(
            hold_created_at=born))
        # A team's ledger begins before its hold does: the grant moves back too.
        await db.execute(update(LedgerEntry).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "grant").values(
            created_at=born - timedelta(days=1)))
        await db.execute(update(Org).where(Org.id == org_id).values(
            spent_today_day=(ledger._now() - timedelta(days=1)).date()))
        await db.commit()


async def test_an_allowance_carried_over_midnight_cannot_take_the_day_past_its_cap(org_id, store):
    """Yesterday's chunk is spent today; the refill that follows must not add another."""
    cap = CHUNK
    epoch = await runtime.take(org_id, 1_000, daily_cap_micro=cap)
    await _finish(org_id, "warm-up", epoch, 1_000, billable=False)
    await _move_hold_to_yesterday(org_id)
    for index in range(4):
        taken = await runtime.take(org_id, CHUNK // 4, daily_cap_micro=cap)
        await _finish(org_id, f"today-{index}", taken, CHUNK // 4, billable=True)
    assert store.counters[org_id]["avail"] == 0
    assert await runtime.take(org_id, 1_000, daily_cap_micro=cap) is None
    async with session_maker() as db:
        assert await ledger.spent_today(db, org_id) == cap
    assert await _state(org_id) == (GRANT - CHUNK, 0)


async def test_an_allowance_already_past_a_lowered_cap_is_drained(org_id, store):
    await runtime.take(org_id, 600, daily_cap_micro=CHUNK)
    await _move_hold_to_yesterday(org_id)
    store.expire()  # the next take refills, and the refill sees the lower cap
    assert await runtime.take(org_id, 600, daily_cap_micro=CHUNK // 2) is None
    assert store.counters[org_id]["avail"] == 0
    assert await _state(org_id) == (GRANT - 600, 600)  # the unfinished take stays in the hold


@pytest.mark.parametrize("switched_off", [False, True])
async def test_the_exact_path_leaves_room_for_an_allowance_carried_over_midnight(
        org_id, store, monkeypatch, switched_off):
    """The hold and its money outlive the switch, so the cap check may not depend on it."""
    caller = await _caller(org_id)
    caller.org.daily_cap_micro = CHUNK + 500
    await runtime.take(org_id, 600, daily_cap_micro=CHUNK + 500)
    await _move_hold_to_yesterday(org_id)
    if switched_off:
        monkeypatch.setattr(get_settings(), "money_allowance_org_ids", [])
        monkeypatch.setattr(kv, "configured", lambda: False)
    with pytest.raises(ReservationFailed) as refused:
        await call_reserve._platform_reserve(
            _call(org_id), caller, CallMeta(tags={"user": "u1"}), "tagged-1", allow_allowance=True)
    assert refused.value.kind == "platform_daily_cap_reached"
    assert await _per_call_holds(org_id) == []
    await call_reserve._platform_reserve(
        _call(org_id, estimate=400), caller, CallMeta(tags={"user": "u1"}), "tagged-2",
        allow_allowance=True)
    assert await _per_call_holds(org_id) == ["tagged-2"]


async def test_charges_still_queued_after_the_switch_is_off_count_toward_the_cap(
        org_id, store, monkeypatch):
    """A day's cap of charges waits to be folded, the allowance is switched off, and its hold
    is gone. The exact path must not admit the cap a second time."""
    caller = await _caller(org_id)
    caller.org.daily_cap_micro = CHUNK
    epoch = await runtime.take(org_id, CHUNK, daily_cap_micro=CHUNK)
    await _finish(org_id, "spent-the-cap", epoch, CHUNK, billable=True)
    await _move_hold_to_yesterday(org_id)
    async with session_maker() as db:
        assert await allowance.committed_today(db, org_id) == CHUNK, "counted once"
    monkeypatch.setattr(get_settings(), "money_allowance_org_ids", [])
    with pytest.raises(ReservationFailed) as refused:
        await call_reserve._platform_reserve(_call(org_id), caller, call_ref="after-1")
    assert refused.value.kind == "platform_daily_cap_reached"
    async with session_maker() as db:  # the hold is closed without folding: charges orphaned
        await db.execute(update(Hold).where(Hold.org_id == org_id).values(amount_micro=0))
        await db.execute(update(Org).where(Org.id == org_id).values(
            balance_micro=Org.balance_micro + CHUNK))
        await db.commit()
    with pytest.raises(ReservationFailed):
        await call_reserve._platform_reserve(_call(org_id), caller, call_ref="after-2")
    assert await _per_call_holds(org_id) == []


# ---- the real store ----------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("TREG_TEST_KV_URL"),
                    reason="set TREG_TEST_KV_URL to a scratch Redis")
async def test_many_calls_at_once_on_a_real_store_are_each_charged_once(org_id, monkeypatch):
    """The whole path against the real counter: concurrent reserves across refills, a mix of
    charged, released and cancelled calls, then the sweep. Every charged call is in the ledger
    once and all other money is back in the balance."""
    real = kv.RedisStore(os.environ["TREG_TEST_KV_URL"])
    monkeypatch.setattr(kv, "store", lambda: real)
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(runtime, "_refused_until", {})
    monkeypatch.setattr(runtime, "_charges", runtime._ChargeWriter())
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 20_000)
    monkeypatch.setattr(runtime, "_refreshing", {})
    monkeypatch.setattr(runtime, "_folding", {})
    caller = await _caller(org_id)

    in_flight = asyncio.Semaphore(40)  # a scratch Redis accepts few connections

    async def one(index: int) -> int:
        async with in_flight:
            mk = _call(org_id)
            await call_reserve._platform_reserve(
                mk, caller, call_ref=f"real-{index}", allow_allowance=True)
            assert mk.allowance_epoch, "the allowance served the call"
            await asyncio.sleep(0)
            if index % 10 == 0:
                await call_settle._finish_cancelled_call(None, mk, f"real-{index}")
                return 0
            if index % 7 == 0:
                return (await call_settle._platform_settle(mk, None, reason="network"))[0]
            return (await call_settle._platform_settle(mk, 200, observed_override=450))[0]

    try:
        charged = await asyncio.gather(*(one(index) for index in range(300)))
        paid = [amount for amount in charged if amount]
        assert set(paid) == {450} and len(paid) == sum(
            1 for index in range(300) if index % 10 and index % 7)
        # A refill asked for ahead of the deadline moves the deadline on when it lands.
        await asyncio.gather(*runtime._refreshing.values(), *runtime._folding.values())
        await _age_heartbeat(org_id, allowance_sweep.idle_after_s() + 5)
        assert await allowance_sweep.sweep_once(session_maker) == 1
        assert await _state(org_id) == (GRANT - 450 * len(paid), 0)
        async with session_maker() as db:
            settles = [entry.call_id for entry in await ledger.entries_of(db, org_id, limit=1000)
                       if entry.kind == "settle"]
            assert (await db.execute(select(CallCharge))).first() is None
        assert len(settles) == len(set(settles)) == len(paid)
        assert await real.allowance_take(org_id, 1) == 0
    finally:
        await real._client.delete(kv._allowance_key(org_id), runtime.money_key(org_id))
        await real.aclose()


# ---- refilling ahead of the deadline -----------------------------------------------------------
async def test_a_call_near_the_deadline_refills_beside_itself_and_keeps_its_take(
        org_id, store, monkeypatch):
    """Calls after it must not find the counter lapsed and wait for a refill, or leave for the
    exact path."""
    assert await runtime.take(org_id, 100), "no counter yet: this call refills in line"
    first_deadline, refills = store.until_ms, runtime.refills(org_id)
    monkeypatch.setattr(runtime, "_ahead", lambda: {"ahead_ms": 3_600_000, "low_micro": 0})
    await asyncio.sleep(0.01)
    assert await runtime.take(org_id, 100)
    assert runtime.refills(org_id) == refills, "the call did not wait for the refill"
    await runtime._refreshing[org_id]
    assert runtime.refills(org_id) == refills + 1 and store.until_ms > first_deadline
    assert store.counters[org_id]["avail"] == CHUNK and store.leases == {}
    assert await _state(org_id) == (GRANT - CHUNK - 200, CHUNK + 200)


async def test_a_counter_running_low_is_refilled_before_it_is_empty(org_id, store, monkeypatch):
    monkeypatch.setattr(runtime, "_ahead", lambda: {"ahead_ms": 0, "low_micro": CHUNK // 4})
    assert await runtime.take(org_id, CHUNK // 2)
    assert org_id not in runtime._refreshing
    assert await runtime.take(org_id, CHUNK // 2 - 100)
    await runtime._refreshing[org_id]
    assert store.counters[org_id]["avail"] == CHUNK


async def test_a_refill_ahead_of_the_deadline_waits_for_a_fold_to_give_up_the_lease(
        org_id, store, monkeypatch):
    assert await runtime.take(org_id, 100)
    refills = runtime.refills(org_id)
    monkeypatch.setattr(runtime, "_ahead", lambda: {"ahead_ms": 3_600_000, "low_micro": 0})
    store.leases[runtime.money_key(org_id)] = "a fold"
    assert await runtime.take(org_id, 100)
    await asyncio.sleep(0.1)
    assert not runtime._refreshing[org_id].done() and runtime.refills(org_id) == refills
    del store.leases[runtime.money_key(org_id)]
    await runtime._refreshing[org_id]
    assert runtime.refills(org_id) == refills + 1


async def test_a_call_waits_for_the_lease_a_fold_holds_instead_of_taking_its_own_hold(
        org_id, store):
    """A fold batch keeps the lease for as long as its transaction runs. A call that finds the
    counter lapsed meanwhile refills once the fold lets go."""
    assert await runtime.take(org_id, 100)
    store.expire()
    key = runtime.money_key(org_id)
    store.leases[key] = "a fold"

    async def fold_ends():
        await asyncio.sleep(0.5)
        del store.leases[key]

    ending = asyncio.create_task(fold_ends())
    assert await runtime.take(org_id, 100), "taken from the allowance after the fold"
    await ending
    assert runtime.refills(org_id) == 2 and store.leases == {}


async def test_a_call_gives_up_on_a_lease_held_too_long(org_id, store, monkeypatch):
    monkeypatch.setattr(runtime, "_REFILL_WAIT_S", 0.05)
    assert await runtime.take(org_id, 100)
    store.expire()
    store.leases[runtime.money_key(org_id)] = "someone slow"
    assert await runtime.take(org_id, 100) is None


async def test_steady_calls_stay_on_the_allowance_while_folds_keep_the_lease(
        org_id, store, monkeypatch):
    """A fold batch that takes a while, a counter that lapses every second, and calls that never
    stop: the shape that once sent every call to the exact path, where the refill then queued
    behind them. No call after the first may leave the allowance."""
    monkeypatch.setattr(runtime, "_ahead", _AHEAD)
    monkeypatch.setattr(get_settings(), "money_allowance_fresh_s", 1)
    monkeypatch.setattr(runtime, "_REFILL_FOLD", 5)
    monkeypatch.setattr(runtime, "_BACKLOG_FOLD", 20)
    monkeypatch.setattr(runtime, "_folding", {})
    fold = allowance.fold_charges_in_transaction

    async def slow_fold(db, org, **kwargs):
        folded = await fold(db, org, **kwargs)
        await asyncio.sleep(0.4)
        return folded

    monkeypatch.setattr(allowance, "fold_charges_in_transaction", slow_fold)
    refill, refresh, in_line, beside = runtime._refill, runtime._refresh, [0], [False]

    async def counted_refill(*args):
        in_line[0] += not beside[0]
        return await refill(*args)

    async def marked_refresh(*args):
        beside[0] = True
        try:
            await refresh(*args)
        finally:
            beside[0] = False

    monkeypatch.setattr(runtime, "_refill", counted_refill)
    monkeypatch.setattr(runtime, "_refresh", marked_refresh)
    stop, left, served = time.monotonic() + 3.5, [], [0]

    async def caller(name: int) -> None:
        index = 0
        while time.monotonic() < stop:
            epoch = await runtime.take(org_id, 100)
            if epoch is None:
                left.append((name, index))
            else:
                served[0] += 1
                await _finish(org_id, f"steady-{name}-{index}", epoch, 100, billable=True)
            index += 1
            await asyncio.sleep(0.005)

    await asyncio.gather(*(caller(name) for name in range(12)))
    await asyncio.gather(*runtime._refreshing.values(), *runtime._folding.values())
    assert left == [] and served[0] > 100, (len(left), served[0])
    assert in_line == [1], "only the first call, which found no counter, refilled in line"
    assert runtime.refills(org_id) >= 4, "each window asked for its own refill"
    balance, hold = await _state(org_id)
    assert store.counters[org_id]["avail"] <= hold


async def test_a_refill_beside_a_large_call_does_not_set_that_calls_amount_aside_again(
        org_id, store, monkeypatch):
    """The call that asked was served. What it cost says nothing about the next call."""
    monkeypatch.setattr(runtime, "_ahead", _AHEAD)
    assert await runtime.take(org_id, 2_500_000)
    await runtime._refreshing[org_id]
    assert store.counters[org_id]["avail"] == CHUNK
    assert await _state(org_id) == (GRANT - 2_500_000 - CHUNK, 2_500_000 + CHUNK)


async def test_a_refill_beside_a_call_that_the_balance_cannot_pay_for_keeps_the_counter(
        org_id, store, monkeypatch):
    """Nothing is added, and nothing is taken away: the allowance still serves what it holds,
    and the call in flight can still give its money back to it."""
    monkeypatch.setattr(runtime, "_ahead", _AHEAD)
    epoch = await runtime.take(org_id, 4_250_000)
    await runtime._refreshing[org_id]
    assert await _state(org_id) == (GRANT - 4_250_000, 4_250_000)
    assert runtime._refused_until.get(org_id, 0) < time.monotonic()
    await _finish(org_id, "cancelled", epoch, 4_250_000, billable=False)
    assert store.counters[org_id]["avail"] == 4_250_000
    assert await runtime.take(org_id, 800_000), "the team's money is all still within reach"


async def test_auto_top_up_is_asked_after_a_refill_that_commits_beside_the_call(
        org_id, store, monkeypatch):
    asked = []

    async def after_refill():
        asked.append(runtime.refills(org_id))

    assert await runtime.take(org_id, 100, after_refill=after_refill)
    assert asked == [], "an in-line refill shows in the count around the take"
    monkeypatch.setattr(runtime, "_ahead", lambda: {"ahead_ms": 3_600_000, "low_micro": 0})
    assert await runtime.take(org_id, 100, after_refill=after_refill)
    assert asked == []
    await runtime._refreshing[org_id]
    assert asked == [2] and store.leases == {}, "asked with the lease already given up"


async def test_a_reserve_asks_auto_top_up_again_when_its_refill_commits_after_it_returned(
        org_id, store, monkeypatch):
    caller = await _caller(org_id)
    caller.org.autotopup_enabled, caller.org.autotopup_consented_at = True, ledger._now()
    seen = []

    async def note(org):
        seen.append((await _state(org))[0])

    monkeypatch.setattr(call_reserve, "_schedule_autotopup_after", note)
    await call_reserve._platform_reserve(
        _call(org_id), caller, call_ref="first", allow_allowance=True)
    assert seen == [GRANT - CHUNK]
    monkeypatch.setattr(runtime, "_ahead", lambda: {"ahead_ms": 3_600_000, "low_micro": 0})
    await call_reserve._platform_reserve(
        _call(org_id), caller, call_ref="second", allow_allowance=True)
    assert seen == [GRANT - CHUNK], "the refill beside this call has not committed yet"
    await runtime._refreshing[org_id]
    assert seen == [GRANT - CHUNK, GRANT - CHUNK - 1_200], "and is seen once it has"


async def test_a_refill_that_waited_its_turn_keeps_to_the_daily_cap_the_team_has_now(
        org_id, store, monkeypatch):
    """The call that asked saw no cap. The team set one while the refill waited for the lease."""
    assert await runtime.take(org_id, 800_000)
    monkeypatch.setattr(runtime, "_ahead", _AHEAD)
    key = runtime.money_key(org_id)
    store.leases[key] = "a fold"
    assert await runtime.take(org_id, 1_000), "low now: a refill is asked for, and waits"
    await asyncio.sleep(0.05)
    async with session_maker() as db:
        await db.execute(update(Org).where(Org.id == org_id).values(daily_cap_micro=CHUNK))
        await db.commit()
    del store.leases[key]
    await runtime._refreshing[org_id]
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK), "nothing past the cap was set aside"


async def test_a_low_counter_asks_again_as_soon_as_a_refill_has_filled_it(
        org_id, store, monkeypatch):
    monkeypatch.setattr(runtime, "_ahead", _AHEAD)
    in_line = runtime.refills(org_id)
    for amount in (800_000, 800_000, 210_000):
        before = runtime.refills(org_id)
        assert await runtime.take(org_id, amount)
        in_line += runtime.refills(org_id) - before
        if (task := runtime._refreshing.get(org_id)) is not None:
            await task
    assert in_line == 1, "only the first call, which found no counter"
    assert store.counters[org_id]["avail"] == CHUNK - 210_000


async def test_a_slow_auto_top_up_read_does_not_hold_back_the_next_refill(
        org_id, store, monkeypatch):
    reading, release = asyncio.Event(), asyncio.Event()

    async def slow():
        reading.set()
        await release.wait()

    assert await runtime.take(org_id, 100)
    monkeypatch.setattr(runtime, "_ahead", lambda: {"ahead_ms": 3_600_000, "low_micro": 0})
    assert await runtime.take(org_id, 100, after_refill=slow)
    first = runtime._refreshing[org_id]
    await reading.wait()
    assert runtime.refills(org_id) == 2 and store.leases == {} and not first.done()
    assert await runtime.take(org_id, 100), "the new deadline asks, and is heard"
    await runtime._refreshing[org_id]
    assert runtime.refills(org_id) == 3
    release.set()
    await first
