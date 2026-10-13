"""The admission middleware in front of the real app: the blocklist, the global ceiling, signals.

Each test sets the policy the way the background refresh would (`config_state._swap`) and the
blocklist the way its sync would (`blocklist_state.refresh_once` from a fake store), so the request
path is exercised exactly as served: it reads only memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg import analytics, crypto
from treg.api import app
from treg.application.ratelimit import (
    background,
    blocklist_state,
    config_state,
    limiter,
    metrics,
    signals,
    signals_flush,
)
from treg.application.ratelimit.limiter import RequestFacts
from treg.bootstrap_ratelimit import RateLimitMiddleware
from treg.config import get_settings
from treg.domain.ratelimit import Caller, RateLimitConfig
from treg.infra import kv, request_scope
from treg.infra.db import session_maker
from treg.models import CreditBlock, SurgeSignal
from tests.fake_rate_limit_store import install, install_blocklist

PEER = "127.0.0.1"  # what httpx's ASGITransport reports as the client
VERSION = 42


@pytest.fixture(autouse=True)
def _fresh():
    get_settings.cache_clear()
    limiter.reset_for_tests()
    background.reset_for_tests()
    yield
    limiter.reset_for_tests()
    background.reset_for_tests()
    get_settings.cache_clear()


_blocklist_mode = ["log_only"]


def use(**update) -> RateLimitConfig:
    """The policy, as the config refresh would set it; `blocklist_mode` goes to the blocklist row."""
    _blocklist_mode[0] = update.pop("blocklist_mode", "log_only")
    held = blocklist_state.current()
    if held is not None:
        install_blocklist(version=held.version, mode=_blocklist_mode[0])
    doc = get_settings().rate_limit_defaults.model_dump(mode="json")
    doc.update(update)
    cfg = RateLimitConfig.model_validate(doc)
    config_state._swap(config_state.ConfigState(cfg, VERSION, "store", False))
    metrics.drain()  # the fixture's own signup requests are not part of the test
    signals.reset_for_tests()
    return cfg


async def _load_blocklist(monkeypatch, *entries, version: int = 5):
    return install_blocklist(*entries, version=version, mode=_blocklist_mode[0])


def _token(client) -> str:
    return client.headers["X-Treg-Token"]


async def _learn(client) -> Caller:
    """The first request of a key teaches the cache who it is."""
    assert (await client.get("/tools")).status_code == 200
    caller = limiter.limiter().cache.get(request_scope.key_hash(_token(client)), now=limiter.limiter()._clock())
    assert caller is not None
    return caller


async def _make_paid(org_id: int) -> None:
    async with session_maker() as db:
        db.add(CreditBlock(id=uuid.uuid4().hex, org_id=org_id, kind="purchased", amount_micro=10_000_000,
                           remaining_micro=10_000_000, stripe_payment_intent=f"pi_{uuid.uuid4().hex}"))
        await db.commit()


def _forget_keys() -> None:
    rl = limiter.limiter()
    rl.cache = type(rl.cache)(ttl_s=60, max_size=100)


def _lines(caplog, name: str) -> list[dict]:
    return [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
            if r.getMessage().startswith(name + " ")]


# ---- the blocklist ------------------------------------------------------------------------------
async def test_a_blocked_key_hash_is_refused_403_before_auth(clients, monkeypatch):
    use(blocklist_mode="enforce_403")
    await _load_blocklist(monkeypatch, ("key_hash", crypto.hash_token(_token(clients)), 3600))
    r = await clients.get("/tools")
    assert r.status_code == 403
    assert r.headers["x-treg-block-reason"] == "treg_blocklist.key_hash"
    assert r.json()["detail"] == {"error": "blocked", "reason": "treg_blocklist.key_hash",
                                  "blocklist_version": 5}
    assert limiter.limiter().cache.get(request_scope.key_hash(_token(clients)), now=1e12) is None


async def test_a_blocked_org_is_refused_after_auth_even_on_a_keys_first_request(clients, monkeypatch):
    use(blocklist_mode="enforce_403")
    caller = await _learn(clients)
    _forget_keys()  # a cold process: the org is only known once auth resolves it
    await _load_blocklist(monkeypatch, ("org_id", str(caller.org_id), 3600))
    r = await clients.get("/tools")
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "treg_blocklist.org_id"
    assert r.headers["x-treg-block-reason"] == "treg_blocklist.org_id"


async def test_a_cached_org_is_refused_before_auth(clients, monkeypatch):
    use(blocklist_mode="enforce_403")
    caller = await _learn(clients)
    await _load_blocklist(monkeypatch, ("org_id", str(caller.org_id), 3600))
    r = await clients.get("/tools")
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "treg_blocklist.org_id"


async def test_log_only_matches_and_logs_but_serves(clients, monkeypatch, caplog):
    use()  # the version row's default: log_only
    caller = await _learn(clients)
    await _load_blocklist(monkeypatch, ("org_id", str(caller.org_id), 3600))
    caplog.set_level(logging.INFO, logger="treg.ratelimit")
    assert (await clients.get("/tools")).status_code == 200
    line = _lines(caplog, "rate_limit_blocklist_match")[-1]
    assert (line["match"], line["mode"], line["enforced"], line["blocklist_version"]) == (
        "org_id", "log_only", False, 5)
    assert line["config_version"] == VERSION
    rows, _ = metrics.drain()
    assert any(r["layer"] == "blocklist" and r["result"] == "log_only" and r["mode"] == "log_only"
               and r["config_version"] == VERSION for r in rows)


async def test_other_orgs_and_expired_entries_do_not_match(clients, monkeypatch):
    use(blocklist_mode="enforce_403")
    caller = await _learn(clients)
    await _load_blocklist(monkeypatch, ("org_id", str(caller.org_id + 1000), 3600),
                          ("key_hash", crypto.hash_token(_token(clients)), -1))
    assert (await clients.get("/tools")).status_code == 200


async def test_blocklist_off_never_matches(clients, monkeypatch):
    use(blocklist_mode="off")
    await _load_blocklist(monkeypatch, ("key_hash", crypto.hash_token(_token(clients)), 3600))
    assert (await clients.get("/tools")).status_code == 200


async def test_server_credentials_are_never_blocklisted(clients, monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "server-side-admin")
    get_settings.cache_clear()
    use(blocklist_mode="enforce_403")
    await _load_blocklist(monkeypatch, ("key_hash", crypto.hash_token("server-side-admin"), 3600))
    assert (await clients.get("/admin/kv", headers={"X-Treg-Token": "server-side-admin"})).status_code == 200


async def test_requests_never_read_the_policy_or_the_blocklist(clients, monkeypatch):
    from sqlalchemy import event
    from treg.application.ratelimit import config_sync
    from treg.infra import db as infra_db
    await _load_blocklist(monkeypatch, ("org_id", "999999", 3600))
    store = install(monkeypatch)
    await config_sync.poll_once()
    seen: list[str] = []
    engine = infra_db._ratelimit_engine.sync_engine
    listener = lambda conn, cursor, statement, *a: seen.append(statement)
    before = dict(store.calls)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        for _ in range(5):
            assert (await clients.get("/tools")).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert not [q for q in seen if "ratelimitconfigversion" in q or "blocklist" in q]
    reads = {k: v - before.get(k, 0) for k, v in store.calls.items() if k.startswith("rl_get")}
    assert not any(reads.values())
    assert blocklist_state.counters()["blocklist_reloads"] == 1  # only the install above


# ---- the global ceiling ---------------------------------------------------------------------------
async def _hold(caller: Caller | None):
    rl = limiter.limiter()
    if caller is not None:
        rl.cache.put(caller, now=rl._clock())
    a = await rl.admit(RequestFacts(ip="198.51.100.7", key_hash=caller.key_id if caller else None), {})
    assert a.verdict.admitted
    return a


async def test_global_ceiling_in_shadow_logs_would_block_with_its_reason(clients, caplog):
    use(mode="shadow", global_concurrency=1)
    await _learn(clients)
    await _hold(None)
    caplog.set_level(logging.INFO, logger="treg.ratelimit")
    assert (await clients.get("/tools")).status_code == 200
    line = _lines(caplog, "rate_limit_would_block")[-1]
    assert (line["layer"], line["reason"], line["mode"], line["config_version"]) == (
        "global_concurrency", "treg_rl.global_concurrency", "shadow", VERSION)
    rows, _ = metrics.drain()
    assert any(r["reason"] == "treg_rl.global_concurrency" and r["result"] == "shadow_reject"
               and r["mode"] == "shadow" for r in rows)


async def test_global_ceiling_in_enforce_answers_429_with_retry_after(clients):
    use(mode="enforce", global_concurrency=1, concurrency_retry_after_s=3)
    await _learn(clients)
    await _hold(None)
    r = await clients.get("/tools")
    assert r.status_code == 429 and r.headers["retry-after"] == "3"
    assert r.headers["x-treg-ratelimit-reason"] == "treg_rl.global_concurrency"
    assert r.json()["detail"]["layer"] == "global_concurrency"
    assert r.json()["detail"]["mode"] == "enforce"


async def test_paid_orgs_keep_the_reserved_global_slots(clients):
    use(mode="enforce", global_concurrency=2, global_paid_reserve=1)
    caller = await _learn(clients)
    await _hold(Caller("u" * 64, 999_001, "someone-unpaid", False, 0))
    assert (await clients.get("/tools")).status_code == 429  # unpaid: only 1 of 2 slots usable
    await _make_paid(caller.org_id)
    _forget_keys()
    assert (await _learn(clients)).paid
    assert (await clients.get("/tools")).status_code == 200  # paid: may use the reserved slot


async def test_the_blocklist_is_checked_before_the_global_ceiling(clients, monkeypatch):
    use(mode="enforce", blocklist_mode="enforce_403", global_concurrency=1)
    await _learn(clients)
    await _hold(None)
    await _load_blocklist(monkeypatch, ("key_hash", crypto.hash_token(_token(clients)), 3600))
    assert (await clients.get("/tools")).status_code == 403


async def test_exempt_paths_and_internal_calls_skip_the_ceiling(clients):
    use(mode="enforce", global_concurrency=1)
    await _learn(clients)
    await _hold(None)
    assert (await clients.get("/tools")).status_code == 429
    assert (await clients.get("/health")).status_code != 429
    token = _token(clients)
    async with AsyncClient(transport=ASGITransport(app=request_scope.internal_app(app, outer_token=token)),
                           base_url="http://treg.internal", headers={"X-Treg-Token": token}) as inner:
        assert (await inner.get("/tools")).status_code == 200
    assert (await clients.get("/tools", headers={"treg.internal": "1"})).status_code == 429


async def test_the_ceiling_slot_is_given_back_when_the_app_raises():
    use(mode="enforce", global_concurrency=1)

    async def boom(scope, receive, send):
        raise RuntimeError("handler failed")

    middleware = RateLimitMiddleware(boom)
    scope = {"type": "http", "path": "/tools", "headers": [], "client": (PEER, 1)}
    for _ in range(3):  # a leaked slot would refuse the second attempt with a 429
        with pytest.raises(RuntimeError):
            await middleware(scope, None, None)
    assert len(limiter.limiter().local) == 0


async def test_store_errors_fail_open_and_cool_down(clients, monkeypatch):
    store = install(monkeypatch)
    use(mode="enforce")
    for _ in range(3):
        assert (await clients.get("/tools")).status_code == 200
    assert store.calls["rl_admit_key"] == 1  # the cooldown kept the outage off later requests
    _, gauge = metrics.drain()
    # The key's first request takes two leases (its IP bucket while unresolved, and the global one).
    assert gauge["store_errors"] == 1 and gauge["local_decisions"] == 4


# ---- surge signals ---------------------------------------------------------------------------------
async def test_signals_record_per_ip_and_org_and_never_refuse(clients):
    use(mode="off")
    caller = await _learn(clients)
    # The proxy appends the real client last; whatever the client wrote before it is ignored.
    await clients.get("/tools", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    await clients.get("/tools", headers={"X-Treg-Token": "not-a-real-key"})
    rows = signals._window.rows
    ip = rows[("ip", PEER, None)]
    assert ip.requests == 2 and ip.status_401 == 1 and ip.unknown_key_requests == 1
    assert ip.orgs == {caller.org_id} and ip.max_in_flight >= 1
    assert rows[("ip", "203.0.113.9", None)].requests == 1
    assert ("ip", "6.6.6.6", None) not in rows  # the client-written entry is never the client
    assert rows[("org", None, caller.org_id)].requests == 2
    assert rows[("ip_org", PEER, caller.org_id)].requests == 1


async def test_signals_flush_on_their_own_pool_with_org_age_and_net_paid(clients, monkeypatch):
    use(mode="off")
    caller = await _learn(clients)
    await _make_paid(caller.org_id)
    await clients.get("/tools")
    opened: list[str] = []
    real = signals_flush.ratelimit_session_maker
    monkeypatch.setattr(signals_flush, "ratelimit_session_maker", lambda: (opened.append("ratelimit"), real())[1])
    assert await signals_flush.flush() == 3
    assert opened == ["ratelimit", "ratelimit"]  # the insert, then the prune in its own transaction
    async with session_maker() as db:
        rows = (await db.execute(select(SurgeSignal))).scalars().all()
    org = next(r for r in rows if r.scope == "org")
    assert (org.org_id, org.requests, org.net_paid_micro) == (caller.org_id, 2, 10_000_000)
    assert org.org_created_at is not None and org.org_slug == caller.org_slug
    ip = next(r for r in rows if r.scope == "ip")
    assert ip.ip == PEER and ip.org_id is None and ip.distinct_orgs == 1 and ip.call_requests == 0
    assert await signals_flush.flush() == 0  # the window was taken


async def test_signals_are_bounded(clients, monkeypatch):
    monkeypatch.setenv("TREG_SURGE_MAX_SUBJECTS", "10")
    get_settings.cache_clear()
    use(mode="off")
    for i in range(15):
        signals.leave(signals.enter(f"10.0.0.{i}", None, None, "/x", unknown=False),
                      status=200, org_id=None, blocked=False)
    assert len(signals._window.rows) == 10 and signals._window.dropped == 5


async def test_an_uncached_key_is_counted_but_never_refused_by_the_ceiling(clients):
    """A key's first request in a process (every key, after a deploy) cannot be starved: refused
    before auth it would never be learned."""
    use(mode="enforce", global_concurrency=1)
    await _hold(None)
    _forget_keys()
    assert (await clients.get("/tools")).status_code == 200
    rows, _ = metrics.drain()
    assert any(r["tier"] == "unknown" and r["result"] == "shadow_reject" and r["mode"] == "enforce"
               for r in rows)
    assert (await clients.get("/tools")).status_code == 429  # known now, and unpaid



async def test_a_failing_prune_never_drops_the_insert_and_is_not_retried_every_minute(clients, monkeypatch):
    use(mode="off")
    await clients.get("/tools")
    calls: list[str] = []
    real = signals_flush.ratelimit_session_maker

    class FailingPrune:
        def __init__(self):
            self.inner = real()

        async def __aenter__(self):
            db = await self.inner.__aenter__()
            original = db.execute

            async def execute(statement, *a, **k):
                if "DELETE" in str(statement).upper():
                    calls.append("delete")
                    raise TimeoutError("statement timeout")
                return await original(statement, *a, **k)

            db.execute = execute
            return db

        async def __aexit__(self, *a):
            return await self.inner.__aexit__(*a)

    monkeypatch.setattr(signals_flush, "ratelimit_session_maker", FailingPrune)
    written = await signals_flush.flush()
    assert written > 0 and calls == ["delete"]
    async with session_maker() as db:
        assert len((await db.execute(select(SurgeSignal))).scalars().all()) == written  # insert kept
    await clients.get("/tools")
    assert await signals_flush.flush() > 0
    assert calls == ["delete"]  # the failed prune is not retried until an hour has passed
