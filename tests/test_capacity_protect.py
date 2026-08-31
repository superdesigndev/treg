"""Step D of docs/PROVIDER-CAPACITY-PLAN.md — Protect: refuse-before-reserve on an exhausted
platform account, the typed `provider_capacity` 503, and the call-path capacity mark.

Tiers 1/2 (an org's own tool or key) never consult any of this."""

from __future__ import annotations

import json
from datetime import timedelta

from httpx import AsyncClient
from sqlmodel import select

from treg import audit, ratestore
from treg.application.call import service as call_service
from treg.application.call import settle as call_settle
from treg.application.call.types import CallFailure
from treg.infra.db import session_maker
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.models import Hold, LedgerEntry
from treg.timeutil import utcnow_naive

from test_marketplace_call import EP, EP_MICRO, PLATFORM_KEYS, _balance, _fake_relay, platform_on  # noqa: F401


async def _publish(provider: str, *, exhausted: bool, hours: float = 1.0, health: str | None = None):
    now = utcnow_naive()
    state = LatestState(provider, 0.0 if exhausted else 500.0, "USD", now, "exact",
                        exhausted_until=(now + timedelta(hours=hours)) if exhausted else None,
                        health=health or ("exhausted" if exhausted else "ok"))
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, provider, state.to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()


async def _rows(model):
    async with session_maker() as db:
        return (await db.execute(select(model))).scalars().all()


async def test_exhausted_platform_account_is_refused_before_any_hold(clients: AsyncClient, platform_on):
    await _publish("tikhub", exhausted=True)
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503, r.text
    assert r.headers["X-Treg-Error"] == "1"
    assert "X-Treg-Call-Id" in r.headers
    body = r.json()["detail"]
    assert body["error"] == "provider_capacity_unavailable" and body["provider"] == "tikhub"
    assert body["endpoint_id"] == EP and body["resets_at"] and "own key" in body["message"]
    assert isinstance(body["alternatives"], list)
    assert await _balance(clients) == before, "no charge"
    assert await _rows(Hold) == [], "refused BEFORE reserve: no hold row ever existed"
    assert {e.kind for e in await _rows(LedgerEntry)} <= {"grant"}, "no reserve/release entry either"
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert row["status_code"] == 503 and row["refused_by"] == "capacity" and row["tool_name"] == EP


async def test_own_key_is_never_affected_by_an_exhausted_platform_account(clients: AsyncClient, platform_on):
    await _publish("tikhub", exhausted=True)
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer MKKEY"


async def test_a_stale_or_ok_view_never_refuses(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    await _publish("tikhub", exhausted=False)
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    now = utcnow_naive()
    stale = LatestState("tikhub", None, "", now - timedelta(hours=9), "stale", health="stale")
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "tikhub", stale.to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200


async def test_a_balance_signature_on_the_platform_key_marks_the_provider_for_the_next_call(
    clients: AsyncClient, platform_on, monkeypatch,
):
    await _publish("tikhub", exhausted=False)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, "this call still relays the vendor's answer unchanged"
    assert "X-Treg-Error" not in r.headers
    assert r.headers["X-Treg-Cost-Micro"] == "0" and await _balance(clients) == before
    async with session_maker() as db:  # the mark landed in ratestore, on its own session
        state = LatestState.from_json(await ratestore.kv_get(db, STATE_NS, "tikhub"))
    assert state.health == "exhausted" and state.is_exhausted() and "balance signature" in state.note
    # …and the NEXT platform call is refused before any hold, even though the view is cached
    capacity_view.invalidate()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"never":"reached"}'))
    r2 = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r2.status_code == 503 and r2.json()["detail"]["error"] == "provider_capacity_unavailable"
    assert await _rows(Hold) == []


async def test_burst_429_and_caller_400_never_mark(clients: AsyncClient, platform_on, monkeypatch):
    await _publish("tikhub", exhausted=False)
    for status, body in ((429, b'{"detail":"slow down"}'), (400, b'{"detail":"bad aweme_id"}')):
        monkeypatch.setattr(call_service, "relay", _fake_relay(status, body))
        r = await clients.get(f"/call/{EP}?aweme_id=7")
        assert r.status_code == status
        async with session_maker() as db:
            state = LatestState.from_json(await ratestore.kv_get(db, STATE_NS, "tikhub"))
        assert state.health == "ok" and not state.is_exhausted()


async def test_quota_429_marks_until_the_reset(clients: AsyncClient, platform_on, monkeypatch):
    await _publish("tikhub", exhausted=False)
    monkeypatch.setattr(call_service, "relay", _fake_relay(429, b'{"detail":"limit reached"}'))

    async def relay_with_retry_after(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        from treg.application.call.types import UpstreamResponse
        async def _s():
            yield b'{"detail":"quota"}'
        async def _c():
            return None
        return UpstreamResponse(429, ((b"retry-after", b"7200"),), _s(), _c)
    monkeypatch.setattr(call_service, "relay", relay_with_retry_after)
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 429
    async with session_maker() as db:
        state = LatestState.from_json(await ratestore.kv_get(db, STATE_NS, "tikhub"))
    assert state.health == "exhausted"
    assert timedelta(hours=1, minutes=55) < (state.exhausted_until - utcnow_naive()) <= timedelta(hours=2)


async def test_a_failed_mark_never_fails_the_call(clients: AsyncClient, platform_on, monkeypatch):
    await _publish("tikhub", exhausted=False)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b"out"))

    async def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(call_settle.capacity_marks, "mark_exhausted", boom)
    # mark_exhausted itself swallows; simulate the seam above it raising to prove the guard
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.headers["X-Treg-Cost-Micro"] == "0"


def test_provider_capacity_is_a_treg_blamed_typed_failure():
    exc = CallFailure("provider_capacity", status_code=503, detail={"error": "x"})
    assert exc.blame == "treg" and exc.status_code == 503
