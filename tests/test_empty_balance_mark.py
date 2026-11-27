"""The balance-empty mark: a team refused for its balance is refused again before the money
queue and the database, for a few seconds at most, and never when the call could be paid."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlmodel import select

from treg.application.call import allowance as allowance_runtime
from treg.application.call import reserve as call_reserve
from treg.application.call import service as call_service
from treg.application.call.intake import CallMeta
from treg.application.call.types import ReservationFailed
from treg.config import Settings, get_settings
from treg.domain import money as ledger
from treg.infra import empty_balance, kv, money_admission
from treg.infra.db import session_maker
from treg.models import CallRecord, Hold

from conftest import drain_background_writes
from test_marketplace_call import EP, platform_on  # noqa: F401 - real call fixture
from test_money_admission import LeaseStore
from test_money_admission_application import _call, _payer, money_db  # noqa: F401
from test_routing import ROUTED, _relay_by_provider, enrichment_on  # noqa: F401

TTL_S = 5


class MarkStore(LeaseStore):
    """The lease store of the admission tests plus the three text calls the mark uses."""

    def __init__(self):
        super().__init__()
        self.texts: dict[str, str] = {}
        self.failing = False
        self.reads = self.writes = self.clears = 0

    async def get_text(self, key):
        self.reads += 1
        if self.failing:
            raise ConnectionError("store down")
        return self.texts.get(key)

    async def set_text(self, key, value, ttl_s):
        self.writes += 1
        if self.failing:
            raise ConnectionError("store down")
        assert ttl_s == TTL_S
        self.texts[key] = value
        return True

    async def clear_flag(self, key):
        self.clears += 1
        if self.failing:
            raise ConnectionError("store down")
        self.texts.pop(key, None)


@pytest.fixture
def store(monkeypatch) -> MarkStore:
    """Production's shape: the mark on, money admission on for every team, one shared store."""
    settings = get_settings()
    monkeypatch.setattr(settings, "money_empty_mark_ttl_s", TTL_S)
    monkeypatch.setattr(settings, "money_admission_enabled", True)
    monkeypatch.setattr(settings, "money_admission_org_ids", [])
    shared = MarkStore()
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "store", lambda: shared)
    monkeypatch.setattr(money_admission, "_local_loop", None)
    monkeypatch.setattr(money_admission, "_windows", {})
    monkeypatch.setattr(money_admission, "_store_down_until", 0.0)
    monkeypatch.setattr(money_admission, "_current", {})
    monkeypatch.setattr(empty_balance, "_suspects", {})
    return shared


class Work:
    """What one reserve cost: pool checkouts and money-lease requests."""

    def __init__(self, shared: MarkStore):
        self.shared, self.checkouts = shared, 0
        self.engine = session_maker.kw["bind"].sync_engine

    def _checkout(self, *_args):
        self.checkouts += 1

    def __enter__(self):
        self.leases_before = len(self.shared.calls)
        event.listen(self.engine, "checkout", self._checkout)
        return self

    def __exit__(self, *_exc):
        event.remove(self.engine, "checkout", self._checkout)
        self.leases = len(self.shared.calls) - self.leases_before


async def _refused(org_id, caller, amount=2_000, ref="c", **kwargs) -> ReservationFailed:
    with pytest.raises(ReservationFailed) as refusal:
        await call_reserve._platform_reserve(_call(org_id, ref, amount), caller, call_ref=ref,
                                             **kwargs)
    assert refusal.value.kind == "insufficient_balance"
    return refusal.value


async def _settle_clears() -> None:
    await asyncio.gather(*empty_balance._clears)


def test_the_mark_is_off_until_the_setting_names_a_lifetime(monkeypatch):
    monkeypatch.delenv("TREG_MONEY_EMPTY_MARK_TTL_S", raising=False)
    assert Settings(_env_file=None).money_empty_mark_ttl_s == 0


async def test_off_every_refusal_takes_the_database_and_carries_retry_after(money_db, store,
                                                                            monkeypatch):
    monkeypatch.setattr(get_settings(), "money_empty_mark_ttl_s", 0)
    org_id, caller = await _payer("mark-off")
    for n in range(2):
        with Work(store) as work:
            refusal = await _refused(org_id, caller, ref=f"off-{n}")
        assert work.checkouts > 0 and work.leases > 0
        assert refusal.retry_after_s == call_reserve.INSUFFICIENT_BALANCE_RETRY_AFTER_S
    assert store.texts == {} and store.reads == store.writes == 0


async def test_a_refused_team_is_refused_again_with_no_lease_and_no_session(money_db, store):
    org_id, caller = await _payer("mark-fast")
    with Work(store) as first:
        slow = await _refused(org_id, caller, ref="slow")
    assert first.checkouts > 0 and first.leases > 0
    assert store.texts == {empty_balance._key(org_id): "2000:1000"}

    with Work(store) as second:
        fast = await _refused(org_id, caller, ref="fast")
    assert (second.checkouts, second.leases) == (0, 0)
    assert fast.status_code == slow.status_code == 402
    assert fast.detail == slow.detail, "the same body, field for field"
    assert fast.retry_after_s == slow.retry_after_s == TTL_S


async def test_the_refusals_it_answers_never_renew_the_mark(money_db, store):
    """Steady traffic: the mark is written once, by the refusal the database decided, so it ends
    at its own lifetime however many calls it answers, and the next call asks the database."""
    org_id, caller = await _payer("mark-steady")
    await _refused(org_id, caller, ref="first")
    for n in range(50):
        with Work(store) as work:
            await _refused(org_id, caller, ref=f"steady-{n}")
        assert (work.checkouts, work.leases) == (0, 0)
    assert store.writes == 1

    store.texts.clear()  # its lifetime ran out
    with Work(store) as after:
        await _refused(org_id, caller, ref="after")
    assert after.checkouts > 0 and after.leases > 0 and store.writes == 2


async def test_a_call_the_balance_can_still_pay_is_not_refused(money_db, store):
    org_id, caller = await _payer("mark-cheaper")
    await _refused(org_id, caller, amount=2_000, ref="big")
    mk = _call(org_id, "small", 100)
    await call_reserve._platform_reserve(mk, caller, call_ref="small")
    async with session_maker() as db:
        assert (await db.get(Hold, "small")).amount_micro == 100


async def test_a_larger_call_is_refused_from_the_mark_too(money_db, store):
    org_id, caller = await _payer("mark-larger")
    await _refused(org_id, caller, amount=2_000, ref="big")
    with Work(store) as work:
        refusal = await _refused(org_id, caller, amount=5_000, ref="bigger")
    assert (work.checkouts, work.leases) == (0, 0)
    assert refusal.detail["estimated_cost_micro"] == 5_000
    assert refusal.detail["balance_micro"] == 1_000


async def _grant(org_id):
    async with session_maker() as db:
        await ledger.grant(db, org_id, amount_micro=10_000, once=False)
        await db.commit()


async def _topup(org_id):
    async with session_maker() as db:
        await ledger.topup(db, org_id, amount_micro=10_000, payment_ref="pi_mark")
        await db.commit()


async def _release(org_id):
    async with session_maker() as db:
        await ledger.release(db, "held")


async def _settle_with_refund(org_id):
    async with session_maker() as db:
        await ledger.settle(db, "held", 1)


async def _close_allowance(org_id):
    """The allowance hold going back to the balance: the hold is opened first, and the mark
    put back, so the close is the one credit under test."""
    from treg.domain.money import allowance as money_allowance

    await _release(org_id)
    await _settle_clears()
    async with session_maker() as db:
        await money_allowance.refill_allowance_in_transaction(db, org_id, 900)
        await db.commit()
    kv.store().texts[empty_balance._key(org_id)] = "950:100"
    async with session_maker() as db:
        await money_allowance.close_allowance_in_transaction(db, org_id, reason="test")
        await db.commit()


@pytest.mark.parametrize("credit", [_grant, _topup, _release, _settle_with_refund,
                                    _close_allowance])
async def test_every_credit_to_the_balance_clears_the_mark(money_db, store, credit):
    org_id, caller = await _payer(f"mark-{credit.__name__.strip('_')}")
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "test.admission", 900, call_id="held")
    await _refused(org_id, caller, amount=950, ref="short")
    key = empty_balance._key(org_id)
    assert key in store.texts

    await credit(org_id)
    await _settle_clears()
    assert key not in store.texts

    mk = _call(org_id, "paid", 950)
    with Work(store) as work:
        await call_reserve._platform_reserve(mk, caller, call_ref="paid")
    assert work.checkouts > 0, "the call after a credit takes the usual road, and is paid"


async def test_a_credit_that_rolls_back_clears_nothing(money_db, store):
    org_id, caller = await _payer("mark-rollback")
    await _refused(org_id, caller, ref="short")
    await _settle_clears()
    before = store.clears
    async with session_maker() as db:
        await ledger.grant(db, org_id, amount_micro=10_000, once=False)
        await db.rollback()
    await _settle_clears()
    assert empty_balance._key(org_id) in store.texts and store.clears == before


async def test_a_refusal_the_database_did_not_confirm_writes_no_mark(money_db, store, monkeypatch):
    """The team's counter may hold the money while its lease is busy: that refusal proves no
    shortfall, so the next call must reach the allowance."""
    async def busy(_org_id, _amount):
        return allowance_runtime.Onto(False)

    monkeypatch.setattr(allowance_runtime, "onto_balance_or_short", busy)
    org_id, caller = await _payer("mark-unconfirmed")
    await _refused(org_id, caller, ref="one")
    assert store.texts == {}
    with Work(store) as work:
        await _refused(org_id, caller, ref="two")
    assert work.checkouts > 0


async def test_a_failing_store_leaves_every_call_on_the_usual_road_and_then_recovers(
        money_db, store):
    """Fail open, with an exit: under steady calls a store that fails on every read, write and
    clear changes no answer and adds no state; once it answers the mark works, and a credit
    still opens the team's calls."""
    org_id, caller = await _payer("mark-store-down")
    await _refused(org_id, caller, ref="seen")
    store.texts.clear()
    store.failing = True
    for n in range(30):
        with Work(store) as work:
            refusal = await _refused(org_id, caller, ref=f"down-{n}")
        assert work.checkouts > 0
        assert refusal.status_code == 402
    assert store.texts == {}

    store.failing = False
    await _refused(org_id, caller, ref="back")
    with Work(store) as work:
        await _refused(org_id, caller, ref="fast-again")
    assert (work.checkouts, work.leases) == (0, 0)

    await _grant(org_id)
    await _settle_clears()
    await call_reserve._platform_reserve(_call(org_id, "paid", 2_000), caller, call_ref="paid")


async def test_a_credit_whose_clear_is_lost_is_bounded_by_the_lifetime(money_db, store):
    org_id, caller = await _payer("mark-missed-clear")
    await _refused(org_id, caller, ref="short")
    store.failing = True
    await _grant(org_id)
    await _settle_clears()
    store.failing = False
    await _refused(org_id, caller, ref="still-marked")  # the few seconds the lifetime allows
    store.texts.clear()  # its lifetime ran out
    await call_reserve._platform_reserve(_call(org_id, "paid", 2_000), caller, call_ref="paid")


async def test_a_team_this_process_never_saw_refused_costs_no_store_read(money_db, store):
    org_id, caller = await _payer("mark-healthy")
    await call_reserve._platform_reserve(_call(org_id, "ok", 100), caller, call_ref="ok")
    assert store.reads == 0


async def _marked(org_id, caller) -> None:
    await _refused(org_id, caller, ref="short")


async def test_a_tagged_call_is_never_answered_from_the_mark(money_db, store):
    org_id, caller = await _payer("mark-tagged")
    await _marked(org_id, caller)
    with Work(store) as work:
        await _refused(org_id, caller, ref="tagged", meta=CallMeta(tags={"user": "u1"}))
    assert work.checkouts > 0


async def test_a_member_with_a_spend_limit_is_never_answered_from_the_mark(money_db, store):
    org_id, caller = await _payer("mark-member")
    await _marked(org_id, caller)
    caller.membership.daily_spend_cap_micro = 1_000_000
    with Work(store) as work:
        await _refused(org_id, caller, ref="capped")
    assert work.checkouts > 0


async def test_a_keyless_wallet_is_never_answered_from_the_mark(money_db, store):
    org_id, caller = await _payer("mark-keyless")
    await _marked(org_id, caller)
    token = call_reserve.keyless_payer.set(True)
    try:
        with Work(store) as work:
            await _refused(org_id, caller, ref="keyless")
    finally:
        call_reserve.keyless_payer.reset(token)
    assert work.checkouts > 0


async def test_a_purchased_credit_only_endpoint_is_never_answered_from_the_mark(
        money_db, store, monkeypatch):
    org_id, caller = await _payer("mark-purchased")
    await _marked(org_id, caller)
    monkeypatch.setattr(call_reserve, "_purchased_only", lambda _endpoint: True)
    with Work(store) as work, pytest.raises(ReservationFailed) as refusal:
        await call_reserve._platform_reserve(_call(org_id, "video", 2_000), caller,
                                             call_ref="video")
    assert work.checkouts > 0 and refusal.value.kind == "free_credit_excludes_video"


async def test_a_team_with_a_daily_limit_is_answered_for_the_marked_amount_only(money_db, store):
    org_id, caller = await _payer("mark-daily-cap")
    caller.org.daily_cap_micro = 1_000_000
    await _refused(org_id, caller, amount=2_000, ref="short")
    with Work(store) as same:
        await _refused(org_id, caller, amount=2_000, ref="same")
    assert (same.checkouts, same.leases) == (0, 0)
    with Work(store) as larger:
        await _refused(org_id, caller, amount=3_000, ref="larger")
    assert larger.checkouts > 0


async def test_a_zero_price_call_is_never_answered_from_the_mark(money_db, store):
    org_id, caller = await _payer("mark-zero")
    await _marked(org_id, caller)
    store.texts[empty_balance._key(org_id)] = "0:0"
    await call_reserve._platform_reserve(_call(org_id, "free", 0), caller, call_ref="free")


async def _drain(clients: AsyncClient) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "drain", 1_000_000, call_id="drain")
    return org_id


@pytest.fixture
def mark_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "money_empty_mark_ttl_s", TTL_S)
    monkeypatch.setattr(empty_balance, "_suspects", {})


async def test_the_call_answers_the_same_402_and_keeps_its_activity_row(
        clients: AsyncClient, platform_on, mark_on):
    org_id = await _drain(clients)
    slow = await clients.get(f"/call/{EP}?aweme_id=7")
    fast = await clients.get(f"/call/{EP}?aweme_id=7")
    assert slow.status_code == fast.status_code == 402
    assert slow.headers["Retry-After"] == fast.headers["Retry-After"] == str(TTL_S)
    assert slow.json() == fast.json()
    assert await kv.store().get_text(empty_balance._key(org_id)) is not None
    await drain_background_writes()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(
            CallRecord.org_id == org_id, CallRecord.status_code == 402))).scalars().all()
    assert [row.refused_by for row in rows] == ["balance", "balance"]

    async with session_maker() as db:
        await ledger.release(db, "drain")
    await _settle_clears()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200


async def test_a_call_on_the_team_s_own_key_ignores_the_mark(
        clients: AsyncClient, platform_on, mark_on):
    await _drain(clients)
    for _ in range(2):
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 402
    sid = (await clients.post("/secrets", json={"name": "own-key", "value": "OWN"})).json()["id"]
    await clients.post("/tools", json={"name": "our-tikhub", "base_url": "https://api.tikhub.io",
                                       "secret_id": sid})
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.json()["auth"] == "Bearer OWN"


async def test_a_routed_call_reports_the_refused_child_as_before(
        clients: AsyncClient, enrichment_on, mark_on, monkeypatch):
    await _drain(clients)
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [(200, {"data": {"email": "should-not-run@example.com"}})],
    }, seen))
    answers = [await clients.post(
        f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,findymail"}) for _ in range(2)]
    assert [r.status_code for r in answers] == [402, 402]
    assert answers[0].json() == answers[1].json()
    assert answers[1].json()["detail"]["error"] == "insufficient_balance"
    assert answers[1].headers["Retry-After"] == str(TTL_S)
    assert seen == []
