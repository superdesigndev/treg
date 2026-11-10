"""Quota admission at the shared call boundary, with synthetic upstreams only."""

from collections import Counter
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from treg import archive
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra import org_endpoint_quota
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry

from test_archive import serve  # noqa: F401
from test_bounceban_async import (
    EMAIL, STATUS, VERIFY, VERIFYING, Upstream, _finished,
    bounceban_on, fast_polls,  # noqa: F401
)
from test_capacity_overflow import (
    O, OK_ANSWER, _exhausted, _orthogonal, _route, overflow_on,  # noqa: F401
)
from test_marketplace_call import EP, EP_MICRO, _balance, _fake_relay, platform_on  # noqa: F401
from test_mcp import _call_tool, mcp_session
from test_route_verify import ASK, FIND, ON, TOMBA_HIT, _relay, verifiers_on  # noqa: F401


def _enable(monkeypatch, limits=None, *, unavailable=False):
    monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", True)
    attempts = Counter()
    used = Counter()

    async def admit(org_id, endpoint_id):
        attempts[endpoint_id] += 1
        if unavailable:
            return "unavailable"
        if endpoint_id not in (limits or {}):
            return "allowed"
        if used[endpoint_id] >= limits[endpoint_id]:
            return "exceeded"
        used[endpoint_id] += 1
        return "allowed"

    monkeypatch.setattr(org_endpoint_quota, "admit", admit)

    async def admit_many(targets):
        before = used.copy()
        for target in dict.fromkeys(targets):
            outcome = await org_endpoint_quota.admit(*target)
            if outcome != "allowed":
                used.clear()
                used.update(before)
                return outcome, target
        return "allowed", None

    monkeypatch.setattr(org_endpoint_quota, "admit_many", admit_many)
    return attempts, used


async def _assert_no_money():
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.kind.in_(["reserve", "release", "settle"])))).scalars().all()
        assert not [entry for entry in entries if entry.kind == "settle"]
        assert sum(entry.amount_micro for entry in entries) == 0


async def _reject_before_send(*args, before_outbound=None, **kwargs):
    assert before_outbound is not None
    await before_outbound()
    raise AssertionError("quota refusal must prevent the upstream send")


@pytest.mark.parametrize("surface", ["call", "catalog/call", "table"])
@pytest.mark.parametrize("unavailable,status,code", [
    (False, 429, "org_endpoint_quota_exceeded"),
    (True, 200, None),
])
async def test_known_exhaustion_refuses_but_unavailable_quota_allows_each_surface(
    clients, monkeypatch, platform_on, surface, unavailable, status, code,
):
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    attempts, _ = _enable(monkeypatch, {EP: 0}, unavailable=unavailable)
    relay = AsyncMock(side_effect=_fake_relay(200) if unavailable else _reject_before_send)
    cache = AsyncMock(return_value=None)
    overflow = AsyncMock(side_effect=AssertionError("overflow must not run"))
    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(archive, "lookup", cache)
    monkeypatch.setattr(O, "maybe_overflow", overflow)
    before = await _balance(clients)
    response = await clients.get(f"/{surface}/{EP}?aweme_id=7")
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["detail"]["error"] == code
    charged = EP_MICRO if unavailable else 0
    assert int(response.headers["X-Treg-Cost-Micro"]) == charged
    assert attempts == {EP: 1}
    assert before - await _balance(clients) == charged
    relay.assert_awaited_once()
    overflow.assert_not_called()
    if unavailable:
        async with session_maker() as db:
            assert (await db.execute(select(Hold))).scalars().all() == []
    else:
        await _assert_no_money()


async def test_mcp_uses_same_quota_gate(clients, monkeypatch, platform_on):
    attempts, _ = _enable(monkeypatch, {EP: 0})
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as client:
        out = await _call_tool(client, "call", {"endpoint_id": EP, "params": {"aweme_id": "7"}}, token=token)
    assert out["status"] == 429, out
    assert out["body"]["detail"]["error"] == "org_endpoint_quota_exceeded"
    assert attempts == {EP: 1}
    await _assert_no_money()


async def test_mcp_allows_unavailable_temporary_quota(clients, monkeypatch, platform_on):
    attempts, used = _enable(monkeypatch, {EP: 0}, unavailable=True)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    before = await _balance(clients)
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as client:
        out = await _call_tool(client, "call", {"endpoint_id": EP, "params": {"aweme_id": "7"}}, token=token)
    assert out["status"] == 200, out
    assert attempts == {EP: 1} and used == {}
    clients.headers["X-Treg-Token"] = token
    assert before - await _balance(clients) == EP_MICRO


async def test_replay_does_not_consume_but_next_logical_call_does(clients, monkeypatch, platform_on):
    attempts, used = _enable(monkeypatch, {EP: 1})
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    url = f"/call/{EP}?aweme_id=7"
    first = await clients.get(url, headers={"Idempotency-Key": "quota-replay"})
    assert first.status_code == 200, first.text
    balance = await _balance(clients)
    replay = await clients.get(url, headers={"Idempotency-Key": "quota-replay"})
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert attempts == used == {EP: 1}
    denied = await clients.get(url)
    assert denied.status_code == 429
    assert attempts == {EP: 2} and used == {EP: 1}
    assert await _balance(clients) == balance


async def test_cache_hit_consumes_one_and_exhaustion_cannot_serve_cache(clients, monkeypatch, serve):
    attempts, used = _enable(monkeypatch, {EP: 2})
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"answer":"synthetic"}'))
    url = f"/call/{EP}?aweme_id=7"
    first = await clients.get(url)
    assert first.status_code == 200
    await archive.drain()
    second = await clients.get(url)
    assert second.status_code == 200 and second.headers["X-Treg-Cache"] == "hit"
    balance = await _balance(clients)
    third = await clients.get(url)
    assert third.status_code == 429
    assert attempts == {EP: 3} and used == {EP: 2}
    assert await _balance(clients) == balance


async def test_cache_hit_remains_available_when_quota_state_is_unknown(clients, monkeypatch, serve):
    _enable(monkeypatch, {EP: 1})
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"answer":"synthetic"}'))
    url = f"/call/{EP}?aweme_id=7"
    assert (await clients.get(url)).status_code == 200
    await archive.drain()
    attempts, used = _enable(monkeypatch, {EP: 0}, unavailable=True)
    relay = AsyncMock(side_effect=AssertionError("cache hit must not send another provider call"))
    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    response = await clients.get(url)
    assert response.status_code == 200 and response.headers["X-Treg-Cache"] == "hit"
    assert attempts == {EP: 1} and used == {}
    assert before - await _balance(clients) == int(response.headers["X-Treg-Cost-Micro"])
    relay.assert_not_awaited()


async def test_no_rule_passes_and_upstream_failure_keeps_admission(clients, monkeypatch, platform_on):
    attempts, _ = _enable(monkeypatch)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert attempts == {EP: 1}
    attempts, used = _enable(monkeypatch, {EP: 1})
    monkeypatch.setattr(call_service, "relay", _fake_relay(500))
    assert (await clients.get(f"/call/{EP}?aweme_id=8")).status_code == 500
    assert (await clients.get(f"/call/{EP}?aweme_id=8")).status_code == 429
    assert used == {EP: 1} and attempts == {EP: 2}


@pytest.mark.parametrize("skip_direct", [False, True])
@pytest.mark.parametrize("limit", [0, 1])
async def test_overflow_checks_original_endpoint_once(clients, monkeypatch, overflow_on, skip_direct, limit):
    await _route(price_micro=3000)
    if skip_direct:
        await _exhausted()
    attempts, used = _enable(monkeypatch, {EP: limit})
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([OK_ANSWER], seen))
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == (200 if limit else 429), response.text
    assert attempts == {EP: 1}
    assert used[EP] == limit and len(seen) == limit
    if not limit:
        await _assert_no_money()


async def test_exhausted_routed_parent_cannot_fallback(
    clients, monkeypatch, verifiers_on,
):
    blocked = FIND
    attempts, used = _enable(monkeypatch, {blocked: 0})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == "org_endpoint_quota_exceeded"
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    assert attempts[blocked] == 1 and used[blocked] == 0
    assert seen == []
    assert await _balance(clients) == before
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert (await db.execute(select(LedgerEntry).where(LedgerEntry.kind == "settle"))).scalars().all() == []


async def test_route_internal_polls_do_not_consume_another_allowance(clients, monkeypatch, bounceban_on, fast_polls):
    attempts, used = _enable(monkeypatch, {VERIFY: 1, STATUS: 1})
    upstream = Upstream(VERIFYING, [VERIFYING, _finished("undeliverable")])
    monkeypatch.setattr(call_service, "relay", upstream)
    response = await clients.post("/call/treg.people.email.verify", json={"email": EMAIL},
                                  headers={"X-Treg-Route-Prefer": "bounceban"})
    assert response.status_code == 200, response.text
    assert upstream.seen == ["submit", "poll", "poll"]
    assert attempts == {"treg.people.email.verify": 1, VERIFY: 1}
    assert used == {VERIFY: 1}


async def test_route_internal_poll_ignores_a_rule_blocking_new_poll_calls(clients, monkeypatch, bounceban_on, fast_polls):
    attempts, _ = _enable(monkeypatch, {STATUS: 0})
    upstream = Upstream(VERIFYING, [_finished("deliverable")])
    monkeypatch.setattr(call_service, "relay", upstream)
    response = await clients.post("/call/treg.people.email.verify", json={"email": EMAIL},
                                  headers={"X-Treg-Route-Prefer": "bounceban"})
    assert response.status_code == 200, response.text
    assert response.json()["output"]["status"] == "deliverable"
    assert attempts[STATUS] == 0
    assert upstream.seen == ["submit", "poll"]


async def test_own_credential_catalog_call_obeys_exact_canonical_quota(clients, monkeypatch):
    await clients.post("/secrets", json={"name": "tikhub", "value": "SYNTHETIC-OWN-KEY"})
    attempts, _ = _enable(monkeypatch, {EP: 0})
    relay = AsyncMock(side_effect=_reject_before_send)
    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["endpoint_id"] == EP
    assert attempts == {EP: 1}
    relay.assert_awaited_once()
    await _assert_no_money()


async def test_provider_burst_retry_is_one_quota_admission(clients, monkeypatch, platform_on):
    from test_capacity_smoothing import _relay_script

    attempts, used = _enable(monkeypatch, {EP: 1})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script([
        (429, ((b"retry-after", b"0"),), b'{"detail":"slow"}'),
        (200, (), b'{"ok":true}'),
    ], seen))
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200, response.text
    assert len(seen) == 2
    assert attempts == used == {EP: 1}
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 429
    assert len(seen) == 2
