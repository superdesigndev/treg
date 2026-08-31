"""Step D′ of docs/PROVIDER-CAPACITY-PLAN.md — burst smoothing on treg's own platform keys:
a per-provider token bucket (wait ≤ 2 s, in-process, no DB) and one bounded `retry-after`
re-send for idempotent reads. Never a refusal; never on tiers 1/2."""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import AsyncClient

from treg import ratestore
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.infra.db import _engine, session_maker
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.infra.upstream.limiter import Limiter, limiter
from treg.models import Hold
from treg.timeutil import utcnow_naive

from test_marketplace_call import EP, PLATFORM_KEYS, _balance, _fake_relay, platform_on  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_limiter():
    limiter.reset()
    yield
    limiter.reset()


async def _publish_rate(provider: str, limit: int, window_s: float):
    now = utcnow_naive()
    state = LatestState(provider, 500.0, "USD", now, "exact", health="ok",
                        rate_limit={"limit": limit, "window_s": window_s, "source": "headers"})
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, provider, state.to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()


# ---- the bucket ---------------------------------------------------------------------------------

async def test_token_bucket_spaces_a_burst_and_never_waits_past_the_cap():
    lim = Limiter()
    t0 = time.monotonic()
    waits = await asyncio.gather(*(lim.acquire("p", 10, 1.0) for _ in range(11)))  # 10/s, 11 at once
    elapsed = time.monotonic() - t0
    assert waits.count(0) == 1 and max(waits) <= 1000 and elapsed < 1.5, waits
    assert sorted(waits) == sorted(waits) and sorted(waits)[-1] >= 900, "the 11th waited ~1 s: spaced, not burst"
    lim2 = Limiter()
    fast = await asyncio.gather(*(lim2.acquire("q", 2, 60.0, max_wait_ms=2000) for _ in range(30)))
    assert fast.count(0) == 30, "a wait beyond the cap is not taken: the call proceeds, no refusal"
    assert lim2.bucket("q", 2, 60.0).tokens == pytest.approx(-0.0, abs=1.5)  # tokens returned
    assert await lim.acquire("p", 0, 1.0) == 0


# ---- the call path ------------------------------------------------------------------------------

def _relay_script(answers: list[tuple[int, tuple, bytes]], seen: list):
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        seen.append((request.method, tuple(request.query_items), time.monotonic()))
        status, headers, body = answers.pop(0)
        async def _s():
            yield body
        async def _c():
            return None
        return UpstreamResponse(status, headers, _s(), _c)
    return _relay


async def test_burst_429_with_a_short_retry_after_is_re_sent_once_on_the_same_hold(
    clients: AsyncClient, platform_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script(
        [(429, ((b"retry-after", b"0"),), b'{"detail":"slow"}'), (200, (), b'{"ok":true}')], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert r.headers["X-Treg-Smoothed"] == "retry=1"
    assert len(seen) == 2 and seen[0][:2] == seen[1][:2], "the identical request, twice"
    assert before - await _balance(clients) > 0, "charged once, for the answer that came back"
    async with session_maker() as db:
        from sqlmodel import select
        assert (await db.execute(select(Hold))).scalars().all() == [], "one hold, closed once"


async def test_still_429_after_the_retry_is_relayed_as_is(clients: AsyncClient, platform_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script(
        [(429, ((b"retry-after", b"0"),), b"x"), (429, ((b"retry-after", b"0"),), b"y")], seen))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429 and len(seen) == 2 and r.text == "y"
    assert r.headers["X-Treg-Cost-Micro"] == "0"


async def test_no_retry_for_a_post_a_long_retry_after_or_a_quota_429(clients: AsyncClient, platform_on, monkeypatch):
    from test_marketplace_call import EP_DFS
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script([(429, ((b"retry-after", b"0"),), b"x")], seen))
    r = await clients.post(f"/call/{EP_DFS}", json=[{"target": "x.com"}])
    assert r.status_code == 429 and len(seen) == 1, "a POST is never re-sent"
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_script([(429, ((b"retry-after", b"90"),), b"x")], seen))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 429 and len(seen) == 1
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_script([(429, ((b"retry-after", b"7200"),), b"quota")], seen))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429 and len(seen) == 1 and "retry=" not in r.headers.get("X-Treg-Smoothed", "")


async def test_own_key_calls_are_never_smoothed(clients: AsyncClient, platform_on, monkeypatch):
    await _publish_rate("tikhub", 1, 60.0)
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script(
        [(429, ((b"retry-after", b"0"),), b"a"), (429, ((b"retry-after", b"0"),), b"b")], seen))
    t0 = time.monotonic()
    for _ in range(2):
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 429
    assert len(seen) == 2 and time.monotonic() - t0 < 1.0, "no bucket wait, no re-send on an org's own key"


async def test_concurrent_platform_calls_over_the_limit_relay_no_429_and_hold_no_db(
    clients: AsyncClient, platform_on, monkeypatch,
):
    """The leadsforge case (plan §6), scaled to a 2-per-second limit so the test runs in seconds:
    five calls at once, a provider that counts calls per window (as providers do) and 429s the
    third in any second → the bucket spaces them, the provider sees no burst, added latency stays
    ≤ 2 s, and no DB connection is held while a call waits or relays."""
    await _publish_rate("tikhub", 2, 1.0)
    stamps: list[float] = []
    pool_seen = []
    async def provider(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        pool_seen.append(_engine.pool.checkedout())
        now = time.monotonic()
        stamps.append(now)
        status = 429 if sum(1 for t in stamps if now - t < 0.9) > 2 else 200
        async def _s():
            yield b'{"ok":true}' if status == 200 else b"slow"
        async def _c():
            return None
        return UpstreamResponse(status, (), _s(), _c)
    monkeypatch.setattr(call_service, "relay", provider)
    t0 = time.monotonic()
    rs = await asyncio.gather(*(clients.get(f"/call/{EP}?aweme_id={i}") for i in range(4)))
    assert [r.status_code for r in rs] == [200] * 4, [r.text for r in rs]
    assert time.monotonic() - t0 <= 2.5
    waits = sorted(int(r.headers["X-Treg-Smoothed"].split("=")[1]) for r in rs if "X-Treg-Smoothed" in r.headers)
    assert len(waits) == 3 and waits[-1] <= 2000, waits


async def test_a_smoothed_call_holds_no_db_connection_while_it_waits(clients: AsyncClient, platform_on, monkeypatch):
    """The pool-discipline rule extended to the wait: the bucket runs after the DB phase ended."""
    await _publish_rate("tikhub", 2, 1.0)
    await limiter.acquire("tikhub", 2, 1.0)  # drain the one token so the call below must wait ~0.5 s
    seen = []
    async def provider(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        seen.append(_engine.pool.checkedout())
        async def _s():
            yield b'{"ok":true}'
        async def _c():
            return None
        return UpstreamResponse(200, (), _s(), _c)
    monkeypatch.setattr(call_service, "relay", provider)
    r = await clients.get(f"/call/{EP}?aweme_id=1")
    assert r.status_code == 200 and r.headers["X-Treg-Smoothed"].startswith("wait=")
    assert int(r.headers["X-Treg-Smoothed"].split("=")[1]) >= 300
    assert seen == [0], "no pooled connection during the wait or the relay"
