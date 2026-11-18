"""The call path's use of a team's allowance: take, refill, finish, and the ways back to the exact path."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

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
from treg.infra import kv
from treg.infra.db import reset_db, session_maker
from treg.domain.identity.access import Caller
from treg.models import CallCharge, Hold, Membership, Org, Tool, User

GRANT = 5_000_000
CHUNK = 1_000_000


class FakeStore:
    """The allowance counter and the refill lease, in memory, with the real store's answers."""

    def __init__(self) -> None:
        self.counters: dict[int, dict] = {}
        self.leases: dict[str, str] = {}
        self.until_ms = 0

    def expire(self) -> None:
        """Move the clock past the deadline: takes stop until an add sets a later one."""
        self.until_ms = 0

    async def allowance_take(self, org_id, amount):
        counter = self.counters.get(org_id)
        if counter is None or time.time() * 1000 > self.until_ms or counter["avail"] < amount:
            return 0
        counter["avail"] -= amount
        return counter["epoch"]

    async def allowance_take_up_to(self, org_id, epoch, amount):
        counter = self.counters.get(org_id)
        if counter is None or counter["epoch"] != epoch:
            return 0
        taken = min(counter["avail"], amount)
        counter["avail"] -= taken
        return taken

    async def allowance_give_back(self, org_id, epoch, amount):
        counter = self.counters.get(org_id)
        if counter is None or counter["epoch"] != epoch:
            return False
        counter["avail"] += amount
        return True

    async def allowance_add(self, org_id, epoch, amount, *, until_ms, ttl_ms):
        counter = self.counters.get(org_id)
        if counter is not None and counter["epoch"] > epoch:
            return "stale"
        if counter is None or counter["epoch"] != epoch:
            counter = self.counters[org_id] = {"epoch": epoch, "avail": 0}
        counter["avail"] += amount
        self.until_ms = max(self.until_ms, until_ms)
        return counter["avail"]

    async def allowance_available(self, org_id):
        return self.counters.get(org_id, {}).get("avail", 0)

    async def allowance_drop(self, org_id):
        counter = self.counters.get(org_id)
        if counter is None:
            return 0
        left, counter["avail"] = counter["avail"], 0
        return left

    async def allowance_retire(self, org_id, epoch, ttl_ms):
        counter = self.counters.get(org_id)
        if counter is not None and counter["epoch"] > epoch:
            return 0
        left = counter["avail"] if counter is not None and counter["epoch"] == epoch else 0
        self.counters[org_id] = {"epoch": epoch + 1, "avail": 0}
        return left

    async def acquire_lease(self, key, token, ttl_ms):
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
    assert epoch and store.counters[org_id] == {"epoch": epoch, "avail": CHUNK - 600}
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


async def test_an_overrun_larger_than_the_counter_leaves_nothing_on_offer(org_id, store, monkeypatch):
    """The hold cannot cover the charge, so the counter must not go on admitting calls."""
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 1_000)
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=1_500) == 1_500
    assert store.counters[org_id]["avail"] == 0
    assert (await _fold(org_id)).consumed_micro == 1_500
    assert await _state(org_id) == (GRANT - 1_500, 0)  # 500 past the hold came out of the balance


async def test_a_counter_left_above_its_hold_is_brought_back_down_by_the_next_refill(
        org_id, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "money_allowance_chunk_micro", 1_000)

    async def unavailable(*args):
        return None

    take_up_to = store.allowance_take_up_to
    monkeypatch.setattr(store, "allowance_take_up_to", unavailable)
    epoch = await runtime.take(org_id, 600)
    await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=1_500)
    assert store.counters[org_id]["avail"] == 400, "the store could not be told"
    monkeypatch.setattr(store, "allowance_take_up_to", take_up_to)
    store.expire()
    assert await runtime.take(org_id, 100) == epoch  # the refill folds, adds 600, then corrects
    balance, hold = await _state(org_id)
    assert hold == 600 and store.counters[org_id]["avail"] == hold - 100


async def test_an_unknown_cost_charges_the_reservation_and_an_overrun_takes_the_difference(
        org_id, store):
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-1", epoch, 600, billable=True) == 600
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-2", epoch, 600, billable=True, actual_micro=900) == 900
    assert store.counters[org_id]["avail"] == CHUNK - 1_500
    assert (await _fold(org_id)).consumed_micro == 1_500
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK - 1_500)


async def test_calls_finishing_together_share_one_insert(org_id, store, monkeypatch):
    writes = []
    write = runtime._ChargeWriter._write

    async def counted(batch):
        writes.append(len(batch))
        await write(batch)

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(counted))
    epochs = [await runtime.take(org_id, 100) for _ in range(40)]
    charged = await asyncio.gather(*(
        _finish(org_id, f"call-{index}", epoch, 100, billable=True, actual_micro=70)
        for index, epoch in enumerate(epochs)))
    assert charged == [70] * 40 and writes == [40]
    assert (await _fold(org_id)).calls == 40


async def test_a_charge_that_cannot_be_written_is_not_charged_and_its_take_is_returned(
        org_id, store, monkeypatch):
    async def broken(batch):
        raise RuntimeError("database down")

    monkeypatch.setattr(runtime._ChargeWriter, "_write", staticmethod(broken))
    epoch = await runtime.take(org_id, 600)
    assert await _finish(org_id, "call-1", epoch, 600, billable=True, actual_micro=450) == 0
    assert store.counters[org_id]["avail"] == CHUNK


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
    assert store.counters[org_id] == {"epoch": epoch + 1, "avail": 0}
    assert await allowance_sweep.sweep_once(session_maker) == 0
    new_epoch = await runtime.take(org_id, 600)
    assert new_epoch > epoch and await _state(org_id) == (GRANT - 450 - CHUNK, CHUNK)


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

    async def late(org, epoch, amount, *, until_ms, ttl_ms):
        fresh_ms = get_settings().money_allowance_fresh_s * 1000
        return await add(org, epoch, amount, until_ms=until_ms - fresh_ms - 1, ttl_ms=ttl_ms)

    monkeypatch.setattr(store, "allowance_add", late)  # as if published a whole window late
    assert await runtime.take(org_id, 600) is None
    assert store.counters[org_id]["avail"] == CHUNK, "the money is there; no call may take it"
    assert await _state(org_id) == (GRANT - CHUNK, CHUNK)


# ---- a daily cap across midnight ---------------------------------------------------------------
async def _move_hold_to_yesterday(org_id: int) -> None:
    async with session_maker() as db:
        await db.execute(update(Hold).where(Hold.org_id == org_id, Hold.allowance.is_(True)).values(
            created_at=ledger._now() - timedelta(days=1)))
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


async def test_the_exact_path_leaves_room_for_an_allowance_carried_over_midnight(org_id, store):
    caller = await _caller(org_id)
    caller.org.daily_cap_micro = CHUNK + 500
    await runtime.take(org_id, 600, daily_cap_micro=CHUNK + 500)
    await _move_hold_to_yesterday(org_id)
    with pytest.raises(ReservationFailed) as refused:
        await call_reserve._platform_reserve(
            _call(org_id), caller, CallMeta(tags={"user": "u1"}), "tagged-1", allow_allowance=True)
    assert refused.value.kind == "platform_daily_cap_reached"
    assert await _per_call_holds(org_id) == []
    await call_reserve._platform_reserve(
        _call(org_id, estimate=400), caller, CallMeta(tags={"user": "u1"}), "tagged-2",
        allow_allowance=True)
    assert await _per_call_holds(org_id) == ["tagged-2"]


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
        await real._client.delete(kv._allowance_key(org_id), runtime._refill_key(org_id))
        await real.aclose()
