"""Influencers Club's fixed-price Orthogonal fallback, exercised through /call/."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from treg import ratestore, worker
from treg.application.call import overflow as O, service as call_service
from treg.config import get_settings
from treg.domain.capacity import routes as R, signatures as S
from treg.domain.capacity import verify as V
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.routes_view import view as routes_view
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.domain.catalog import store as catalog_store
from treg.infra.db import reset_db, session_maker
from treg.models import LedgerEntry, OverflowRoute, OverflowSpend
from treg.timeutil import utcnow_naive

from test_capacity_overflow import _holds, _orthogonal, _rows, overflow_on  # noqa: F401
from test_marketplace_call import _balance, _fake_relay, platform_on  # noqa: F401

FIX = Path(__file__).parent / "fixtures" / "aggregators"
EVIDENCE = json.loads((FIX / "verification" / "influencersclub.json").read_text())
VERIFIED_AT = datetime.fromisoformat(EVIDENCE["verified_at"])
SEARCH = "influencersclub.creators.search"
SIMILAR = "influencersclub.creators.similar"
QUOTA = b'{"error":"Discovery API credit limit reached. Credits reset on subscription renewal."}'
ENVELOPE = json.loads((FIX / "orthogonal_influencersclub_search.json").read_text())["body"]


@pytest.fixture
def influencers_on(monkeypatch, overflow_on):  # noqa: F811
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "influencersclub")
    monkeypatch.setenv("TREG_PLATFORM_KEY_INFLUENCERSCLUB", "DIRECT-KEY")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _sync():
    seed = [r for r in R.load_seed() if r["provider"] == "influencersclub"]
    async with session_maker() as db:
        result = await R.apply_sync(db, seed, catalog=catalog_store.load(), now=VERIFIED_AT)
        await db.commit()
    routes_view.invalidate()
    capacity_view.invalidate()
    return result


def _body(endpoint=SEARCH, limit=2):
    if endpoint == SIMILAR:
        return {"platform": "instagram", "filter_key": "username", "filter_value": "nasa",
                "paging": {"page": 0, "limit": limit}}
    return {"platform": "instagram", "filters": {"keywords_in_bio": ["astronomy"]},
            "paging": {"limit": limit, "page": 0}}


def test_discovery_quota_is_exhaustion_but_burst_and_validation_are_not():
    quota = S.classify("influencersclub", 429, {}, QUOTA)
    assert quota.kind == "quota" and quota.resets_at is None and S.is_exhausting(quota)
    assert S.classify("influencersclub", 402, {}, b'{}').kind == "balance"
    burst = S.classify("influencersclub", 429, httpx.Headers({"Retry-After": "42"}),
                       b'{"error":"Too many requests. Please try again later.","retry_after":42}')
    assert burst.kind == "burst" and not S.is_exhausting(burst)
    assert S.classify("influencersclub", 400, {}, b'{"message":"Invalid platform value"}') is None
    assert S.classify("influencersclub", 403, {}, b'{"detail":"Not allowed for current plan"}') is None


@pytest.mark.parametrize("changes,reason", [
    ({"aggregator": "monid"}, "unit mismatch"),
    ({"agg_path": "/unverified/"}, "unit mismatch"),
    ({"agg_price_micro": 30_001}, "verified ceiling"),
    ({"agg_price_micro": None}, "missing"),
    ({"last_verified_at": None}, "never verified"),
])
async def test_fixed_discovery_exception_does_not_bypass_contract_or_price_checks(changes, reason):
    await reset_db()
    await _sync()
    r = next(r for r in await _rows(OverflowRoute) if r.endpoint_id == SEARCH)
    for k, v in changes.items():
        setattr(r, k, v)
    result = R.eligible(r, our_cost={"type": "per_result", "per": 1},
                        platform_eligible=True, policy=None, now=VERIFIED_AT, our_usd=0.00598)
    assert not result.enabled and reason in result.reason


@pytest.mark.parametrize("endpoint", [SEARCH, SIMILAR])
@pytest.mark.parametrize("status,error", [(402, b'{"detail":"Insufficient credits"}'), (429, QUOTA)])
async def test_exhaustion_releases_parent_and_charges_flat_orthogonal_price(
    clients, influencers_on, monkeypatch, endpoint, status, error,
):
    await _sync()
    monkeypatch.setattr(call_service, "relay", _fake_relay(status, error))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, ENVELOPE)], seen))
    before = await _balance(clients)
    body = _body(endpoint, limit=10)
    response = await clients.post(f"/call/{endpoint}", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == ENVELOPE["data"]
    assert response.headers["X-Treg-Served-Via"] == "overflow:orthogonal"
    assert response.headers["X-Treg-Cost-Micro"] == "30000"
    assert before - await _balance(clients) == 30_000 and await _holds() == []
    assert len(seen) == 1 and seen[0].json["api"] == "influencers-club"
    assert seen[0].json["body"] == body
    parent = response.headers["X-Treg-Call-Id"]
    entries = [(r.kind, r.call_id) for r in await _rows(LedgerEntry) if r.kind != "grant"]
    assert sorted(entries) == sorted([
        ("reserve", parent), ("release", parent),
        ("reserve", parent + ":overflow"), ("settle", parent + ":overflow"),
    ])
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1 and spend[0].cost_micro == 30_000 and spend[0].calls == 1


@pytest.mark.parametrize("limit,expected_status", [(1, 200), (2, 200)])
async def test_known_empty_account_skips_direct_and_serves_any_page_size_via_overflow(
    clients, influencers_on, monkeypatch, limit, expected_status,
):
    """2026-09-17: a one-creator request used to fail the 4x ratio guard and get a typed 503 telling
    the caller to bring their own key; the flat $0.03 is now admitted by its absolute ceiling."""
    await _sync()
    now = utcnow_naive()
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "influencersclub", LatestState(
            "influencersclub", 0.0, "credits", now, "exact",
            exhausted_until=now + timedelta(hours=1), health="exhausted").to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()
    async def no_direct(*args, **kwargs):
        raise AssertionError("known exhausted account must not be called")
    monkeypatch.setattr(call_service, "relay", no_direct)
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, ENVELOPE)], seen))
    response = await clients.post(f"/call/{SEARCH}", json=_body(limit=limit),
                                  headers={"Idempotency-Key": "discovery-empty"})
    assert response.status_code == expected_status, response.text
    entries = [r for r in await _rows(LedgerEntry) if r.kind != "grant"]
    assert len(seen) == 1 and response.headers["X-Treg-Cost-Micro"] == "30000"
    assert await _holds() == []
    assert sorted(r.kind for r in entries) == ["reserve", "settle"]
    replay = await clients.post(f"/call/{SEARCH}", json=_body(limit=limit),
                                headers={"Idempotency-Key": "discovery-empty"})
    assert replay.status_code == 200 and replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert len(seen) == 1


@pytest.mark.parametrize("case,status", [("own_key", 402), ("opt_out", 402), ("validation", 400)])
async def test_no_fallback_or_charge_when_call_is_ineligible(
    clients, influencers_on, monkeypatch, case, status,
):
    await _sync()
    if case == "own_key":
        response = await clients.post("/secrets", json={"name": "influencersclub", "value": "OWN-KEY"})
        assert response.status_code < 300
    if case == "opt_out":
        org = (await clients.get("/orgs")).json()[0]["org_id"]
        response = await clients.patch(f"/orgs/{org}/settings", json={"platform_overflow": False})
        assert response.status_code == 200
    error = b'{"message":"Invalid platform value"}' if status == 400 else b'{"detail":"Insufficient credits"}'
    monkeypatch.setattr(call_service, "relay", _fake_relay(status, error))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, ENVELOPE)], seen))
    before = await _balance(clients)
    response = await clients.post(f"/call/{SEARCH}", json=_body(limit=2))
    assert response.status_code == status and response.content == error
    assert seen == [] and before == await _balance(clients) and await _holds() == []


async def test_worker_verifies_only_selected_provider(clients, influencers_on, monkeypatch):
    # The real worker verifies production configuration; PostgreSQL requires a persistent key.
    monkeypatch.setattr(get_settings(), "secret_key", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    await _sync()
    from test_capacity_overflow import _route
    await _route()  # unrelated enabled route, below the cap: it must never be contacted
    seen = []
    async def verify(client, route, **kwargs):
        seen.append(route.endpoint_id)
        return V.Verification(route.endpoint_id, route.aggregator, 200, 200, True,
                              route.agg_price_micro, utcnow_naive())
    monkeypatch.setattr(V, "verify_route", verify)
    result = await worker._overflow_verify(SimpleNamespace(all=True, only="influencersclub", max_usd=0.66))
    assert result == 0 and len(seen) == 10
    assert all(e.startswith("influencersclub.") for e in seen)


async def test_discovery_cancellation_releases_both_holds_and_keeps_unknown_spend(
    clients, influencers_on, monkeypatch,
):
    await _sync()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{}'))
    async def cancelled(client, req):
        raise asyncio.CancelledError()
    monkeypatch.setattr(O, "_send", cancelled)
    before = await _balance(clients)
    with pytest.raises(asyncio.CancelledError):
        await clients.post(f"/call/{SEARCH}", json=_body())
    assert before == await _balance(clients) and await _holds() == []
    entries = [r.kind for r in await _rows(LedgerEntry) if r.kind != "grant"]
    assert sorted(entries) == ["release", "release", "reserve", "reserve"]
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1 and spend[0].cost_micro == 30_000
