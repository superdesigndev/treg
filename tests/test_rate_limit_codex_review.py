"""Reproductions for the Codex review of #934; each failed on the code before its fix."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager

import anyio
import pytest
from sqlalchemy import event

from treg import crypto
from treg.application.ratelimit import background, config_state, limiter
from treg.bootstrap_ratelimit import RateLimitMiddleware
from treg.config import get_settings
from treg.domain.ratelimit import RateLimitConfig
from treg.infra import db as infra_db
from treg.infra import kv
from tests.fake_rate_limit_store import install, install_blocklist

ADMIN = "codex-admin"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    limiter.reset_for_tests()
    background.reset_for_tests()
    yield
    limiter.reset_for_tests()
    background.reset_for_tests()
    get_settings.cache_clear()


def use(**update) -> RateLimitConfig:
    doc = get_settings().rate_limit_defaults.model_dump(mode="json")
    doc.update(update)
    cfg = RateLimitConfig.model_validate(doc)
    config_state._swap(config_state.ConfigState(cfg, 42, "store", False))
    return cfg


def _hold_global() -> None:
    """Occupy the one global slot the way an in-flight request would (per-process ceiling)."""
    rl = limiter.limiter()
    rl.local.admit(member="held", cap=1, ttl_ms=600_000, retry_ms=1000, enforce=True,
                   now_ms=int(rl._clock() * 1000))


@contextmanager
def _db_checkouts():
    seen: list[str] = []
    engines = {id(e): e for e in (infra_db._engine, infra_db._admin_engine,
                                  infra_db._background_engine, infra_db._ratelimit_engine)}

    def checkout(*_):
        seen.append("checkout")

    for engine in engines.values():
        event.listen(engine.sync_engine, "checkout", checkout)
    try:
        yield seen
    finally:
        for engine in engines.values():
            event.remove(engine.sync_engine, "checkout", checkout)


# ---- 1. the limiter's identity is the credential auth resolved, nothing else ------------------------
async def test_a_header_auth_ignores_cannot_reset_the_limit(clients):
    use(mode="enforce", global_concurrency=1)
    assert (await clients.get("/tools")).status_code == 200  # the key is learned
    _hold_global()
    assert (await clients.get("/tools")).status_code == 429
    for junk in ({"X-Treg-Org": "anything"}, {"X-Treg-Org": "x" * 40}, {"X-Unrelated": "1"}):
        r = await clients.get("/tools", headers=junk)
        assert r.status_code == 429, (junk, r.status_code)


async def test_a_blocked_key_with_extra_headers_is_refused_without_any_db_auth(clients):
    assert (await clients.get("/tools")).status_code == 200
    org = (await clients.get("/orgs")).json()[0]
    org_id = org.get("org_id") or org.get("id")
    install_blocklist(("org_id", str(org_id), 3600), version=3, mode="enforce_403")
    assert (await clients.get("/tools")).status_code == 403
    for junk in ({"X-Treg-Org": "some-other-team"}, {"X-Treg-Org": "zzz"}, {"X-Whatever": "1"}):
        with _db_checkouts() as seen:
            r = await clients.get("/tools", headers=junk)
        assert r.status_code == 403, (junk, r.status_code)
        assert seen == [], junk


class Gate:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await self.release.wait()
        await send({"type": "http.response.body", "body": b""})


async def _call(mw, headers=(), path="/tools", peer="10.0.0.5"):
    sent: list = []

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "GET", "path": path, "client": (peer, 1),
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    await mw(scope, None, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_a_spoofed_forwarded_for_does_not_change_the_ip_bucket():
    """No credential: a low per-IP bucket keyed by the hop the platform's proxy appends (the last
    X-Forwarded-For entry), never by what the client wrote before it."""
    use(mode="enforce", anonymous_ip_concurrency=1)
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    held = asyncio.create_task(_call(mw, [("x-forwarded-for", "1.1.1.1, 9.9.9.9")]))
    await asyncio.sleep(0)
    assert await asyncio.wait_for(_call(mw, [("x-forwarded-for", "8.8.8.8, 9.9.9.9")]), 1) == 429
    assert await asyncio.wait_for(_call(mw, [("x-forwarded-for", "9.9.9.9")]), 1) == 429
    gate.release.set()
    await held
    gate.release = asyncio.Event()
    other = asyncio.create_task(_call(mw, [("x-forwarded-for", "9.9.9.9, 7.7.7.7")]))
    await asyncio.sleep(0)
    gate.release.set()
    assert await other == 200  # the real client is 7.7.7.7: its own bucket


# ---- 2. Postgres is the policy's source; Redis never serves an older version -------------------------
async def _refresh():
    try:
        from treg.application.ratelimit import config_sync
    except ImportError:  # before the fix, workers read the Redis copy
        return await config_state.refresh_once()
    return await config_sync.poll_once()


def _policy(**update) -> dict:
    doc = get_settings().rate_limit_defaults.model_dump(mode="json")
    doc.update(update)
    return doc


async def test_a_stale_republish_cannot_overwrite_a_newer_revert(clients, monkeypatch):
    store = install(monkeypatch)
    from treg.application.ratelimit import admin as rl_admin
    h = {"X-Treg-Token": ADMIN}
    v1 = (await clients.put("/admin/rate-limit/config", headers=h, json={
        "config": _policy(mode="enforce", global_concurrency=50), "note": "go",
        "enforce_approval": "ok"})).json()["version"]
    stale = await _row(v1)  # what a republish read from Postgres before the revert committed
    v2 = (await clients.post("/admin/rate-limit/config/revert-to-shadow", headers=h)).json()["version"]

    real_latest = rl_admin._latest

    async def stale_read(db):
        return stale

    monkeypatch.setattr(rl_admin, "_latest", stale_read)  # ...and it publishes only now
    await clients.post("/admin/rate-limit/config/republish", headers=h)
    monkeypatch.setattr(rl_admin, "_latest", real_latest)
    assert json.loads(store.docs[kv.RL_CONFIG_KEY])["version"] == v2  # the cache kept the newer one
    await _refresh()
    state = config_state.current()
    assert (state.version, state.config.mode) == (v2, "shadow")


async def test_after_a_redis_flush_a_stale_writer_is_never_served(clients, monkeypatch):
    store = install(monkeypatch)
    from treg.application.ratelimit import admin as rl_admin
    h = {"X-Treg-Token": ADMIN}
    await clients.put("/admin/rate-limit/config", headers=h, json={
        "config": _policy(mode="enforce", global_concurrency=50), "note": "go", "enforce_approval": "ok"})
    v2 = (await clients.post("/admin/rate-limit/config/revert-to-shadow", headers=h)).json()["version"]
    store.docs.clear()  # Redis restarted empty
    old = RateLimitConfig.model_validate(_policy(mode="enforce", global_concurrency=50))
    await rl_admin._publish(1, old, actor="stale-writer")  # a slow writer lands late
    await _refresh()
    state = config_state.current()
    assert (state.version, state.config.mode) == (v2, "shadow")


async def _row(version):
    from treg.infra.db import session_maker
    from treg.models import RateLimitConfigVersion
    async with session_maker() as db:
        return await db.get(RateLimitConfigVersion, version)


# ---- 4. a cancelled request gives its slot back ----------------------------------------------------
class SlowReleaseStore:
    """Ceiling calls that yield to the loop, as a real network store does."""

    def __init__(self) -> None:
        self.members: set[str] = set()

    async def rl_admit_key(self, key, member, cap, ttl_ms, enforce):
        await asyncio.sleep(0)
        n = len(self.members) + 1
        ok = cap is None or n <= cap
        if ok or not enforce:
            self.members.add(member)
        return ok or not enforce, ok, n

    async def rl_admit_global(self, member, cap, ttl_ms, enforce):
        return await self.rl_admit_key(kv.RL_GLOBAL_KEY, member, cap, ttl_ms, enforce)

    async def rl_release_key(self, key, member):
        await asyncio.sleep(0)
        self.members.discard(member)

    async def rl_release(self, member):
        await self.rl_release_key(kv.RL_GLOBAL_KEY, member)

    async def rl_renew(self, key, members):
        await asyncio.sleep(0)


async def test_a_cancelled_request_releases_its_slot_even_under_level_cancellation(monkeypatch, clients):
    store = SlowReleaseStore()
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "_store", store)
    use(mode="enforce", global_concurrency=1)
    assert (await clients.get("/tools")).status_code == 200  # learn the key
    token = clients.headers["X-Treg-Token"]

    async def forever(scope, receive, send):
        await asyncio.Event().wait()

    mw = RateLimitMiddleware(forever)
    async with anyio.create_task_group() as tg:
        tg.start_soon(_call, mw, [("x-treg-token", token)])
        await asyncio.sleep(0.05)
        assert len(store.members) == 1
        tg.cancel_scope.cancel()  # anyio cancels every await inside the scope, including `finally`
    assert store.members == set()


# ---- 5. a long-lived request keeps its slot past the lease TTL -------------------------------------
async def test_a_long_lived_request_keeps_counting_past_the_lease_ttl(clients, monkeypatch):
    monkeypatch.setenv("TREG_RATE_LIMIT_KEY_CACHE_TTL_S", "3600")  # the fake clock must not expire the key
    get_settings.cache_clear()
    limiter.reset_for_tests()
    cfg = use(mode="enforce", global_concurrency=1, concurrency_ttl_s=180)
    assert (await clients.get("/tools")).status_code == 200
    token = clients.headers["X-Treg-Token"]
    rl = limiter.limiter()
    clock = [time.time()]
    rl._clock = lambda: clock[0]
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    stream = asyncio.create_task(_call(mw, [("x-treg-token", token)]))
    await asyncio.sleep(0)
    for _ in range(4):  # 4 x 100 s: well past the 180 s lease, renewed by the heartbeat
        clock[0] += 100
        await rl.renew_leases()
    assert await asyncio.wait_for(_call(mw, [("x-treg-token", token)]), 1) == 429  # still counted
    # Without renewal the same wait would have aged the lease out: the TTL is still the backstop.
    rl._live.clear()
    clock[0] += 200
    late = asyncio.create_task(_call(mw, [("x-treg-token", token)]))
    await asyncio.sleep(0)
    assert not late.done()  # admitted: the unrenewed lease aged out
    gate.release.set()
    await asyncio.gather(stream, late)
    assert cfg.concurrency_ttl_s == 180
