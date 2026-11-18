"""One team's money path under concurrency against real loopback PostgreSQL (and Redis for the
admission runs), never production: calls reserve and close while the lazy reap and the scheduled
sweep race for the same stale holds. Opt in with TREG_TEST_DB_URL (and TREG_TEST_KV_URL)."""

from __future__ import annotations

import asyncio
import os
import time
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
from treg.infra import kv, money_admission
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


@pytest.mark.parametrize("admission", ["off", "on", "store-down"])
async def test_concurrent_calls_and_reapers_keep_drift_zero_and_close_each_hold_once(
        local_postgres, monkeypatch, admission):
    org_id, caller = await _team()
    store = await _admission_on(monkeypatch, org_id) if admission != "off" else None
    offline = None
    if admission == "store-down":
        # Admission on, Redis unreachable for the whole run: every reserve, close and sweep release
        # falls back to the database without the team's lock, under continuous traffic.
        offline = kv.RedisStore("redis://127.0.0.1:1/0")
        assert not await offline.ping()
        monkeypatch.setattr(kv, "store", lambda: offline)
        monkeypatch.setattr(money_admission, "_store_down_until", 0.0)
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
    await hold_sweep.reap_stale_holds()
    if offline is not None:
        await offline.aclose()
        assert any(row["fallback_kv_unavailable"] + row["fallback_kv_store_down"]
                   for row in money_admission.snapshot())
        # The store is back: once the skip window has passed, the next reserve is admitted by
        # Redis again, so the fallback ends by itself.
        monkeypatch.setattr(kv, "store", lambda: store)
        later = money_admission._clock() + money_admission._STORE_DOWN_S + 1
        monkeypatch.setattr(money_admission, "_clock", lambda: later)
        mk, after = _call(org_id), f"after-{uuid4().hex}"
        await call_reserve._platform_reserve(mk, caller, call_ref=after)
        assert (await call_settle._platform_settle(mk, 200, observed_override=70))[0] == 70
        settled.append(after)
        assert any(row["operation"] == "reserve" and row["mode"] == "redis" and row["completed"]
                   for row in money_admission.snapshot())
    if store is not None:
        await store.aclose()

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


class _Proxy:
    """TCP proxy to Redis. mode: up | refuse (connections reset) | blackhole (accept, never answer)."""

    def __init__(self, target_port: int):
        self.target_port, self.mode, self.conns = target_port, "up", set()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def set(self, mode: str):
        self.mode = mode
        if mode != "up":
            for w in list(self.conns):
                w.transport.abort()
            self.conns.clear()

    async def _handle(self, r, w):
        if self.mode == "refuse":
            w.transport.abort()
            return
        if self.mode == "blackhole":
            self.conns.add(w)
            await asyncio.sleep(3600)
            return
        ur, uw = await asyncio.open_connection("127.0.0.1", self.target_port)
        self.conns.update({w, uw})

        async def pipe(a, b):
            try:
                while data := await a.read(65536):
                    if self.mode != "up":
                        break
                    b.write(data)
                    await b.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                b.transport.abort()
        await asyncio.gather(pipe(r, uw), pipe(ur, w))


@pytest.mark.parametrize("down_mode", ["refuse", "blackhole"])
async def test_redis_down_then_back_under_continuous_traffic(local_postgres, monkeypatch, down_mode):
    """Real clock, real Redis behind a proxy that goes away and comes back: the fallback must end by
    itself while calls and the sweep keep running, and every hold must close exactly once."""
    org_id, caller = await _team()
    real = await _admission_on(monkeypatch, org_id)
    real_port = int(get_settings().kv_url.rsplit(":", 1)[1].split("/")[0])
    proxy = _Proxy(real_port)
    await proxy.start()
    store = kv.RedisStore(f"redis://127.0.0.1:{proxy.port}/0")
    monkeypatch.setattr(kv, "store", lambda: store)
    monkeypatch.setattr(money_admission, "_store_down_until", 0.0)
    monkeypatch.setattr(get_settings(), "reserve_reaps_stale_holds", False)   # the sweep does it
    stale = await _stale_holds(org_id)
    money_admission.snapshot()
    settled: list[str] = []
    released: list[str] = []
    errors: list[tuple[str, str]] = []
    stop = asyncio.Event()
    modes: list[tuple[float, str, int]] = []
    t0 = time.monotonic()

    async def worker(w: int):
        n = 0
        while not stop.is_set():
            mk, call_id = _call(org_id), f"c-{w}-{n}-{uuid4().hex}"
            n += 1
            try:
                await call_reserve._platform_reserve(mk, caller, call_ref=call_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(("reserve", type(exc).__name__))
                continue
            if n % 3 == 0:
                assert (await call_settle._platform_settle(mk, 503))[0] == 0
                released.append(call_id)
            else:
                assert (await call_settle._platform_settle(mk, 200, observed_override=70))[0] == 70
                settled.append(call_id)

    async def sweeper():
        while not stop.is_set():
            await hold_sweep.reap_stale_holds(batch_size=3)
            await asyncio.sleep(0.05)

    async def sampler():
        while not stop.is_set():
            await asyncio.sleep(1.0)
            for row in money_admission.snapshot():
                if row["operation"] == "reserve" and row["completed"]:
                    modes.append((round(time.monotonic() - t0, 1), row["mode"], row["completed"]))

    tasks = [asyncio.create_task(worker(w)) for w in range(8)]
    tasks += [asyncio.create_task(sweeper()), asyncio.create_task(sampler())]
    await asyncio.sleep(3)
    proxy.set(down_mode)
    down_at = time.monotonic() - t0
    await asyncio.sleep(10)
    proxy.set("up")
    up_at = time.monotonic() - t0
    await asyncio.sleep(14)
    stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 60)
    await hold_sweep.reap_stale_holds()
    await store.aclose()
    await real.aclose()

    assert not errors, Counter(errors)
    tail = [m for m in modes if m[0] >= up_at + 8]
    assert tail and all(mode == "redis" for _t, mode, _n in tail), "fallback did not end"
    down = [m for m in modes if down_at + 1 <= m[0] <= up_at]
    assert down and any(mode == "fallback" for _t, mode, _n in down)

    async with session_maker() as db:
        blocks = await ledger.blocks_of(db, org_id)
        holds = await ledger.open_holds_of(db, org_id)
        balance = await ledger.balance_of(db, org_id)
        entries = (await db.execute(select(LedgerEntry.call_id, LedgerEntry.kind)
                                    .where(LedgerEntry.org_id == org_id,
                                           LedgerEntry.call_id.is_not(None)))).all()
    assert holds == []
    assert balance == sum(b.remaining_micro for b in blocks)
    assert balance == FUNDS - 70 * len(settled)
    kinds = Counter(entries)
    for c in settled:
        assert (kinds[c, "reserve"], kinds[c, "settle"], kinds[c, "release"]) == (1, 1, 0)
    for c in released + stale:
        assert (kinds[c, "reserve"], kinds[c, "settle"], kinds[c, "release"]) == (1, 0, 1)
