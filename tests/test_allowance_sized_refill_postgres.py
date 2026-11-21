"""A busy team's allowance against real PostgreSQL and Redis, with money admission on for every
team: refills sized to what the team spends come far less often than a fixed chunk, and every
charge is still folded exactly once.

Opt in with TREG_TEST_DB_URL and TREG_TEST_KV_URL naming disposable local instances.
"""

from __future__ import annotations

import asyncio
import os
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
SECONDS = 6
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
                        ("money_admission_org_ids", []), ("money_allowance_org_ids", [])):
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


async def _team(monkeypatch, teams: list[int], name: str) -> tuple[int, Caller]:
    async with session_maker() as db:
        org = Org(id=int(uuid4().hex[:7], 16) + 1, name=name, slug=f"{name}-{uuid4().hex}")
        db.add(org)
        await db.flush()
        await ledger.grant(db, org.id, amount_micro=GRANT)
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


async def _busy(org_id: int, caller: Caller, tag: str) -> list[tuple[int, bool]]:
    """CALLS_PER_S calls a second for SECONDS seconds through the call path's own reserve and
    settle, with the background loop folding as it does in production. (charged, served by
    the allowance) per call."""
    loop = asyncio.get_running_loop()

    async def one(index: int) -> tuple[int, bool]:
        mk = _call(org_id)
        await call_reserve._platform_reserve(
            mk, caller, call_ref=f"{tag}-{index}", allow_allowance=True)
        served = bool(mk.allowance_epoch)
        await asyncio.sleep(0.2)  # the provider answering
        return (await call_settle._platform_settle(mk, 200, observed_override=COST))[0], served

    folding = asyncio.create_task(allowance_sweep.worker(fold_every_s=0.25))
    try:
        start, calls = loop.time(), []
        for index in range(CALLS_PER_S * SECONDS):
            await asyncio.sleep(max(0.0, start + index / CALLS_PER_S - loop.time()))
            calls.append(asyncio.create_task(one(index)))
        results = await asyncio.gather(*calls)
        await asyncio.gather(*runtime._refreshing.values())
    finally:
        folding.cancel()
        await asyncio.gather(folding, return_exceptions=True)
    return results


async def _books(store, org_id: int, results: list[tuple[int, bool]]) -> None:
    """Every charge folded exactly once, the invariant holds, and once the idle hold is closed
    the team has paid exactly what its calls cost."""
    paid = [charged for charged, _ in results]
    assert paid == [COST] * len(results) and all(served for _, served in results)
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
    real, teams = store
    fixed_team, caller = await _team(monkeypatch, teams, "fixed-chunk")
    await _books(real, fixed_team, await _busy(fixed_team, caller, "fixed"))
    monkeypatch.setattr(get_settings(), "money_allowance_refill_window_s", 30)
    paced_team, caller = await _team(monkeypatch, teams, "paced")
    await _books(real, paced_team, await _busy(paced_team, caller, "paced"))
    fixed, paced = runtime.refills(fixed_team), runtime.refills(paced_team)
    assert fixed >= 4 and paced * 3 <= fixed, f"fixed chunk: {fixed} refills, paced: {paced}"
