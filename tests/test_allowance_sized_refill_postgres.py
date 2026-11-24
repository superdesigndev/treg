"""A busy team's allowance against real PostgreSQL and Redis, with money admission on for every
team: refills sized to what the team spends come far less often than a fixed chunk, a refill
whose read the store does not answer costs no more than it does for the chunk and the team is
back on the allowance once the store answers, and every charge is still folded exactly once.

Opt in with TREG_TEST_DB_URL and TREG_TEST_KV_URL naming disposable local instances.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from treg.application import allowance_sweep
from treg.application.call import allowance as runtime
from treg.application.call import reserve as call_reserve
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.application.call.types import ReservationFailed
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.identity.access import Caller
from treg.infra import kv, money_admission
from treg.infra.db import reset_db, session_maker
from treg.models import CallCharge, Hold, Membership, Org, Tool, User

GRANT = 500_000_000
PRICE = 100_000  # what each call sets aside
COST = 80_000  # what each call turns out to cost
CALLS_PER_S = 10
_LOCAL = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
async def store(monkeypatch):
    engine, url = session_maker.kw["bind"], os.environ.get("TREG_TEST_KV_URL")
    if engine.dialect.name != "postgresql" or not url:
        pytest.skip("requires disposable PostgreSQL and Redis (TREG_TEST_DB_URL, TREG_TEST_KV_URL)")
    assert engine.url.host in _LOCAL and urlsplit(url).hostname in _LOCAL, "local services only"
    await reset_db()
    real = kv.RedisStore(url)
    settings = get_settings()
    for name, value in (("platform_margin", 0), ("kv_url", url), ("money_admission_enabled", True),
                        ("money_admission_org_ids", []), ("money_allowance_org_ids", []),
                        ("money_allowance_all_orgs", False), ("money_allowance_excluded_org_ids", [])):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(kv, "store", lambda: real)
    monkeypatch.setattr(money_admission, "_store_down_until", 0.0)
    for name in ("_refused_until", "_refreshing", "_refresh_again", "_freezes_due",
                 "_freeze_waiters", "_freeze_tasks", "_freeze_wake"):
        monkeypatch.setattr(runtime, name, {})
    monkeypatch.setattr(runtime, "_after_refills", set())
    monkeypatch.setattr(runtime, "_in_flight", [0])
    monkeypatch.setattr(runtime, "_charges", runtime._ChargeWriter())
    teams: list[int] = []
    yield real, teams
    for org_id in teams:
        await real._client.delete(kv._allowance_key(org_id), runtime.money_key(org_id),
                                  f"money-admission:org:{org_id}")
    await real.aclose()


async def _team(monkeypatch, teams: list[int], name: str,
                grant: int = GRANT) -> tuple[int, Caller]:
    async with session_maker() as db:
        org = Org(id=int(uuid4().hex[:7], 16) + 1, name=name, slug=f"{name}-{uuid4().hex}")
        db.add(org)
        await db.flush()
        await ledger.grant(db, org.id, amount_micro=grant)
        user = User(email=f"{uuid4().hex}@example.invalid")
        db.add(user)
        await db.flush()
        membership = Membership(user_id=user.id, org_id=org.id, role="owner",
                                token_hash=uuid4().hex)
        db.add(membership)
        await db.commit()
        for row in (org, user, membership):
            await db.refresh(row)
            db.expunge(row)
    settings = get_settings()
    monkeypatch.setattr(settings, "money_allowance_org_ids",
                        [*settings.money_allowance_org_ids, org.id])
    teams.append(org.id)
    return org.id, Caller(membership, user, org, None)


def _call(org_id: int) -> MarketplaceCall:
    return MarketplaceCall(
        tool=Tool(org_id=org_id, name="test", owner="test@example.invalid",
                  base_url="https://example.invalid", host="example.invalid"),
        upstream="https://example.invalid/call", consumed=set(), endpoint_id="test.allowance",
        provider="test", tier="platform", cost_type="per_call", estimate_micro=PRICE,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": PRICE})


async def _busy(org_id: int, caller: Caller, tag: str,
                seconds: int) -> tuple[list[tuple[float, int, bool]], int]:
    """CALLS_PER_S calls a second for `seconds` through the call path's own reserve and settle,
    with the background loop folding as it does in production. (when it started, charged,
    served by the allowance) per call, and the team's refills when half the calls had started."""
    loop = asyncio.get_running_loop()
    start = loop.time()

    async def one(index: int) -> tuple[float, int, bool]:
        mk = _call(org_id)
        await call_reserve._platform_reserve(
            mk, caller, call_ref=f"{tag}-{index}", allow_allowance=True)
        served = bool(mk.allowance_epoch)
        await asyncio.sleep(0.2)  # the provider answering
        charged = (await call_settle._platform_settle(mk, 200, observed_override=COST))[0]
        return index / CALLS_PER_S, charged, served

    folding = asyncio.create_task(allowance_sweep.worker(fold_every_s=0.25))
    total, halfway = CALLS_PER_S * seconds, 0
    try:
        calls = []
        for index in range(total):
            await asyncio.sleep(max(0.0, start + index / CALLS_PER_S - loop.time()))
            if index == total // 2:
                halfway = runtime.refills(org_id)
            calls.append(asyncio.create_task(one(index)))
        results = await asyncio.gather(*calls)
        await asyncio.gather(*runtime._refreshing.values())
    finally:
        folding.cancel()
        await asyncio.gather(folding, return_exceptions=True)
    return results, halfway


async def _books(store, org_id: int, results: list[tuple[float, int, bool]], *,
                 served_at_least: float = 0.9) -> None:
    """Every charge folded exactly once, the invariant holds, and once the idle hold is closed
    the team has paid exactly what its calls cost. A call the exact path served is charged
    once there."""
    paid = [charged for _, charged, _ in results]
    assert paid == [COST] * len(results)
    served = sum(served for *_, served in results)
    assert served >= served_at_least * len(results), "the allowance served them"
    while await allowance_sweep.fold_queued_once(session_maker):
        pass
    async with session_maker() as db:
        holds = await ledger.open_holds_of(db, org_id)
        assert [hold.allowance for hold in holds] == [True]
        assert (await store.allowance_state(org_id))[0] <= holds[0].amount_micro
    async with session_maker() as db:
        await db.execute(update(Hold).where(Hold.org_id == org_id).values(
            heartbeat_at=ledger._now() - timedelta(seconds=allowance_sweep.idle_after_s() + 5)))
        await db.commit()
    assert await allowance_sweep.sweep_once(session_maker) >= 1
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        blocks = sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id))
        assert await ledger.open_holds_of(db, org_id) == []
        assert balance == blocks == GRANT - sum(paid), "nothing created, nothing lost"
        assert await ledger.spent_today(db, org_id) == await ledger.spent_today_from_ledger(
            db, org_id)
        settles = [entry for entry in await ledger.entries_of(db, org_id, limit=10_000)
                   if entry.kind == "settle"]
        assert (await db.execute(
            select(CallCharge.call_id).where(CallCharge.org_id == org_id))).first() is None
    assert len(settles) == len({entry.call_id for entry in settles}) == len(paid)
    assert sorted(entry.amount_micro for entry in settles) == [-COST] * len(paid)


async def test_a_busy_team_refills_far_less_often_when_refills_follow_its_spend(store, monkeypatch):
    """A sized refill at most doubles what the counter spent since the last one, so it takes a
    few refills to reach the window; past those, the chunk keeps going back to the team's money
    lease about once a second and the sized counter about once per half lifetime."""
    real, teams = store
    fixed_team, caller = await _team(monkeypatch, teams, "fixed-chunk")
    results, fixed_half = await _busy(fixed_team, caller, "fixed", 12)
    await _books(real, fixed_team, results)
    monkeypatch.setattr(get_settings(), "money_allowance_refill_window_s", 30)
    paced_team, caller = await _team(monkeypatch, teams, "paced")
    results, paced_half = await _busy(paced_team, caller, "paced", 12)
    await _books(real, paced_team, results)
    fixed, paced = runtime.refills(fixed_team), runtime.refills(paced_team)
    counts = f"fixed chunk: {fixed_half} then {fixed} refills, paced: {paced_half} then {paced}"
    assert fixed - fixed_half >= 4 and paced - paced_half <= 1, counts
    assert paced * 2 <= fixed, counts


async def test_a_refill_whose_read_stalls_holds_nothing_up_once_the_store_answers(
        store, monkeypatch):
    """The store does not answer the read a sized refill makes (it carries the pace with the
    state) for three and a half seconds of steady calls. Each refill gives up at the store's
    timeout, as a refill to the chunk does when its state read stalls, and the calls that need
    one take the exact path. Once the store answers, every call is back on the allowance, the
    background loop has folded every charge, and the books are exact."""
    real, teams = store
    monkeypatch.setattr(get_settings(), "money_allowance_refill_window_s", 30)
    team, caller = await _team(monkeypatch, teams, "stalled")
    loop, stalled = asyncio.get_running_loop(), [0]
    began = loop.time()
    answer = real._client.eval

    async def eval_(script, *args):
        if script == kv._ALLOWANCE_STATE_AND_PACE and 1.5 <= loop.time() - began < 5.0:
            stalled[0] += 1
            await asyncio.sleep(1.5)
        return await answer(script, *args)

    monkeypatch.setattr(real._client, "eval", eval_)
    results, _ = await _busy(team, caller, "stalled", 9)
    assert stalled[0] >= 1, "the stall was met"
    # A read that began just before the store answered again gives up at its timeout, about a
    # second later; from then on every call is served by the allowance.
    assert [started for started, _, served in results if started >= 6.5 and not served] == []
    assert any(not served for _, _, served in results), "the calls that needed a refill left"
    await _books(real, team, results, served_at_least=0.5)


class _Process:
    """One process's store: its own client, noting what each give-back asks of it."""

    def __init__(self, url: str, noting: contextvars.ContextVar) -> None:
        self.client, self._noting = kv.RedisStore(url), noting

    def __getattr__(self, name):
        operation = getattr(self.client, name)
        if not callable(operation):
            return operation

        async def noted(*args, **kwargs):
            if (names := self._noting.get()) is not None:
                names.append(name)
            return await operation(*args, **kwargs)
        return noted


async def test_refusals_of_a_team_out_of_money_cost_one_store_read_each(store, monkeypatch):
    """A team that has run out of money keeps calling from three processes at once, on the
    exact path and on its allowance, which keeps asking for a refill it cannot have. Each
    refusal reads the team's counter once, asks for no lease and waits for nothing: a refusal
    costs what it did before, plus one read."""
    real, teams = store
    monkeypatch.setattr(get_settings(), "money_allowance_refill_window_s", 30)
    team, caller = await _team(monkeypatch, teams, "broke", grant=50_000)
    process, noting = contextvars.ContextVar("process", default=0), contextvars.ContextVar(
        "noting", default=None)
    processes = [_Process(os.environ["TREG_TEST_KV_URL"], noting) for _ in range(3)]
    monkeypatch.setattr(kv, "store", lambda: processes[process.get()])
    give_back, asked = runtime.give_back_for_exact_path, []

    async def timed(org_id, short_micro):
        names, began = [], time.monotonic()
        token = noting.set(names)
        try:
            return await give_back(org_id, short_micro)
        finally:
            noting.reset(token)
            asked.append((names, time.monotonic() - began))

    monkeypatch.setattr(runtime, "give_back_for_exact_path", timed)

    async def calls(index: int) -> None:
        process.set(index)
        for n in range(20):
            with pytest.raises(ReservationFailed) as refused:
                await call_reserve._platform_reserve(
                    _call(team), caller, call_ref=f"broke-{index}-{n}",
                    allow_allowance=n % 2 == 0)
            assert refused.value.status_code == 402
            await asyncio.sleep(0.005)

    try:
        await asyncio.gather(*(calls(index) for index in range(3)))
    finally:
        for each in processes:
            await each.client.aclose()
    assert len(asked) == 60 and {tuple(names) for names, _ in asked} == {("allowance_state",)}
    assert max(waited for _, waited in asked) < 0.25, "no lease waited for"
    async with session_maker() as db:
        assert await ledger.balance_of(db, team) == 50_000
        assert await ledger.open_holds_of(db, team) == []


async def test_a_give_back_that_cannot_have_the_teams_admission_in_time_changes_nothing(
        store, monkeypatch):
    """Another process holds the team's money admission. The give-back waits for it no longer
    than a call waits for a refill, and the counter, which it empties only once admitted, still
    offers what it held."""
    real, teams = store
    monkeypatch.setattr(get_settings(), "money_allowance_refill_window_s", 30)
    team, _ = await _team(monkeypatch, teams, "admission", grant=5_000_000)
    assert await runtime._refill(team, 0, 0)
    before = await real.allowance_state(team)
    admission = f"money-admission:org:{team}"
    assert await real.acquire_lease(admission, "another process", 10_000) == "acquired"
    try:
        began = time.monotonic()
        assert await runtime.give_back_for_exact_path(team, 500_000) is False
        assert time.monotonic() - began < runtime._REFILL_WAIT_S + 0.5
    finally:
        await real.release_lease(admission, "another process")
    assert await real.allowance_state(team) == before
    assert await real.acquire_lease(runtime.money_key(team), "probe", 1_000) == "acquired"
    await real.release_lease(runtime.money_key(team), "probe")
    async with session_maker() as db:
        released = [entry for entry in await ledger.entries_of(db, team, limit=100)
                    if entry.kind == "release"]
    assert released == []
