"""One team's money path under concurrency against real loopback PostgreSQL (and Redis for the
admission run), never production: calls reserve and close while the lazy reap and the scheduled
sweep race for the same stale holds. Opt in with TREG_TEST_DB_URL (and TREG_TEST_KV_URL)."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from treg.application import hold_sweep
from treg.application.call import reserve as call_reserve
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.identity.access import Caller
from treg.infra import kv
from treg.infra.db import reset_db, session_maker
from treg.models import Hold, LedgerEntry, Membership, Org, Tool, User
from treg.timeutil import utcnow_naive

FUNDS = 10_000_000
WORKERS, CALLS_EACH, STALE = 12, 15, 20


@pytest.fixture
async def local_postgres(monkeypatch):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires disposable PostgreSQL via TREG_TEST_DB_URL")
    assert engine.url.host in {"127.0.0.1", "localhost", "::1"}, "local test DB only"
    await reset_db()
    monkeypatch.setattr(get_settings(), "platform_margin", 0)


async def _admission_on(monkeypatch, org_id: int) -> kv.RedisStore:
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    store = kv.RedisStore(url)
    assert await store.ping()
    settings = get_settings()
    monkeypatch.setattr(settings, "kv_url", url)
    monkeypatch.setattr(kv, "store", lambda: store)
    monkeypatch.setattr(settings, "money_admission_enabled", True)
    monkeypatch.setattr(settings, "money_admission_org_ids", [org_id])
    monkeypatch.setattr(settings, "money_admission_wait_s", 20.0)
    return store


async def _team() -> tuple[int, Caller]:
    slug = f"money-path-{uuid4().hex}"
    async with session_maker() as db:
        # A distinct id also isolates Redis keys across parallel test databases.
        org = Org(id=int(uuid4().hex[:7], 16) + 1, name=slug, slug=slug)
        db.add(org)
        await db.flush()
        await ledger.grant(db, org.id, amount_micro=FUNDS)
        user = User(email=f"{slug}@example.invalid")
        db.add(user)
        await db.flush()
        membership = Membership(user_id=user.id, org_id=org.id, role="owner", token_hash=slug)
        db.add(membership)
        await db.commit()
        for row in (org, user, membership):
            await db.refresh(row)
            db.expunge(row)
        return org.id, Caller(membership, user, org, None)


async def _stale_holds(org_id: int) -> list[str]:
    ids = [f"stale-{n}-{uuid4().hex}" for n in range(STALE)]
    async with session_maker() as db:
        for call_id in ids:
            await ledger.reserve_in_transaction(db, org_id, "test.money_path", 100, call_id=call_id)
        await db.execute(update(Hold).where(Hold.id.in_(ids)).values(
            created_at=utcnow_naive() - timedelta(seconds=ledger.hold_ttl_s() + 5)))
        await db.commit()
    return ids


def _call(org_id: int) -> MarketplaceCall:
    return MarketplaceCall(
        tool=Tool(org_id=org_id, name="test", owner="test@example.invalid",
                  base_url="https://example.invalid", host="example.invalid"),
        upstream="https://example.invalid/call", consumed=set(), endpoint_id="test.money_path",
        provider="test", tier="platform", cost_type="per_call", estimate_micro=100,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": 100},
    )


@pytest.mark.parametrize("admission", [False, True], ids=["db-only", "admission"])
async def test_concurrent_calls_and_reapers_keep_drift_zero_and_close_each_hold_once(
        local_postgres, monkeypatch, admission):
    org_id, caller = await _team()
    store = await _admission_on(monkeypatch, org_id) if admission else None
    stale = await _stale_holds(org_id)
    monkeypatch.setattr(get_settings(), "reserve_reaps_stale_holds", True)
    settled: list[str] = []
    released: list[str] = []
    done = asyncio.Event()

    async def caller_task(worker: int) -> None:
        for n in range(CALLS_EACH):
            mk = _call(org_id)
            call_id = f"call-{worker}-{n}-{uuid4().hex}"
            await call_reserve._platform_reserve(mk, caller, call_ref=call_id)
            if n % 3 == 2:
                assert (await call_settle._platform_settle(mk, 503))[0] == 0
                released.append(call_id)
            else:
                assert (await call_settle._platform_settle(mk, 200, observed_override=70))[0] == 70
                settled.append(call_id)

    async def sweeper() -> None:
        while not done.is_set():
            await hold_sweep.reap_stale_holds(batch_size=3)
            await asyncio.sleep(0)

    sweep = asyncio.create_task(sweeper())
    try:
        async with asyncio.timeout(120):
            await asyncio.gather(*(caller_task(n) for n in range(WORKERS)))
    finally:
        done.set()
        await sweep
        if store is not None:
            await store.aclose()
    await hold_sweep.reap_stale_holds()

    async with session_maker() as db:
        blocks = await ledger.blocks_of(db, org_id)
        holds = await ledger.open_holds_of(db, org_id)
        balance = await ledger.balance_of(db, org_id)
        entries = (await db.execute(select(LedgerEntry.call_id, LedgerEntry.kind, LedgerEntry.meta)
                                    .where(LedgerEntry.org_id == org_id,
                                           LedgerEntry.call_id.is_not(None)))).all()
    assert holds == []
    assert balance - (sum(b.remaining_micro for b in blocks)) == 0   # drift, with no open holds
    assert balance == FUNDS - 70 * len(settled)
    kinds = Counter((call_id, kind) for call_id, kind, _meta in entries)
    for call_id in settled:
        assert (kinds[call_id, "reserve"], kinds[call_id, "settle"], kinds[call_id, "release"]) == (1, 1, 0)
    for call_id in released + stale:
        assert (kinds[call_id, "reserve"], kinds[call_id, "settle"], kinds[call_id, "release"]) == (1, 0, 1)
    reaped = {call_id for call_id, kind, meta in entries
              if kind == "release" and (meta or {}).get("reason") == "stale_hold_reaped"}
    assert reaped == set(stale)
