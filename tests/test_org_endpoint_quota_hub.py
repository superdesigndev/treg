"""Exact endpoint protection and the existing hub partial-failure billing contract."""

from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import funded_user
from sqlmodel import select

from treg import ratestore
from treg.application.call import overflow as O, route as call_route, service as call_service
from treg.config import get_settings
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing.plan import Candidate, Plan
from treg.infra import org_endpoint_quota
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry
from treg.timeutil import utcnow_naive

from test_capacity_overflow import overflow_on  # noqa: F401
from test_hub import _steps_manifest, hub_on  # noqa: F401
from test_influencersclub_overflow import SEARCH, _body, _sync, influencers_on  # noqa: F401
from test_marketplace_call import EP, _balance, _fake_relay, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _reject_before_send
from test_route_verify import ASK, FIND


def _block(monkeypatch, org_id, *, unavailable=False):
    monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", True)
    attempts = Counter()

    async def admit(caller_org, endpoint_id):
        attempts[endpoint_id] += 1
        if caller_org == org_id and endpoint_id == SEARCH:
            return "unavailable" if unavailable else "exceeded"
        return "allowed"

    monkeypatch.setattr(org_endpoint_quota, "admit", admit)

    async def admit_many(targets):
        for target in dict.fromkeys(targets):
            outcome = await admit(*target)
            if outcome != "allowed":
                return outcome, target
        return "allowed", None

    monkeypatch.setattr(org_endpoint_quota, "admit_many", admit_many)
    return attempts


async def _money():
    async with session_maker() as db:
        holds = (await db.execute(select(Hold))).scalars().all()
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    return holds, entries


@pytest.mark.parametrize("skip_direct", [False, True])
async def test_discovery_quota_precedes_direct_and_orthogonal(
    clients, monkeypatch, influencers_on, skip_direct,
):
    await _sync()
    if skip_direct:
        now = utcnow_naive()
        async with session_maker() as db:
            await ratestore.kv_put(db, STATE_NS, "influencersclub", LatestState(
                "influencersclub", 0.0, "credits", now, "exact",
                exhausted_until=now + timedelta(hours=1), health="exhausted",
            ).to_json(), ttl_s=3600)
            await db.commit()
        capacity_view.invalidate()
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    attempts = _block(monkeypatch, org_id)
    direct = AsyncMock(side_effect=_reject_before_send)
    overflow = AsyncMock(side_effect=AssertionError("blocked endpoint reached Orthogonal"))
    monkeypatch.setattr(call_service, "relay", direct)
    monkeypatch.setattr(O, "_send", overflow)
    before = await _balance(clients)
    response = await clients.post(f"/call/{SEARCH}", json=_body())
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == "org_endpoint_quota_exceeded"
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    assert attempts == {SEARCH: 1}
    assert await _balance(clients) == before
    if skip_direct:
        direct.assert_not_called()
    else:
        direct.assert_awaited_once()
    overflow.assert_not_called()
    holds, entries = await _money()
    assert not holds
    assert not [entry for entry in entries if entry.kind == "settle"]
    assert sum(entry.amount_micro for entry in entries
               if entry.kind in {"reserve", "release"}) == 0


@pytest.mark.parametrize("unavailable", [False, True])
async def test_discovery_in_a_synthetic_route_plan_has_endpoint_local_quota(
    clients, monkeypatch, influencers_on, unavailable,
):
    """Discovery currently has no waterfall; inject only a plan to test the shared child road."""
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "influencersclub,tikhub")
    get_settings.cache_clear()
    catalog = catalog_store.load()
    candidates = [Candidate(
        endpoint=catalog.by_id[endpoint_id],
        adapter=SimpleNamespace(
            to_upstream=lambda identity, variant, endpoint=endpoint_id: (
                ({"aweme_id": "7"}, None) if endpoint == EP else ({}, _body())),
            from_upstream=lambda doc: {"email": "synthetic@example.com", "verified": True},
            is_miss=lambda doc: False,
        ),
        variant=(), tier="platform", price_micro=1000, hit_rate=None,
        ok_rate=None, p50_ms=None, last_ok_days=None,
    ) for endpoint_id in (SEARCH, EP)]
    monkeypatch.setattr(call_route, "build_plan", AsyncMock(return_value=Plan(
        contract=catalog.contracts["people.email.find"], identity={}, variant=(), candidates=candidates,
    )))
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    attempts = _block(monkeypatch, org_id, unavailable=unavailable)
    sent = []

    async def send(request, upstream_url, *args, **kwargs):
        if before_outbound := kwargs.pop("before_outbound", None):
            await before_outbound()
        sent.append(upstream_url)
        body = (b'{"accounts":[{"user_id":"synthetic"}],"total":1,"credits_cost":0.01}'
                if "influencers.club" in upstream_url else b'{"data":"synthetic"}')
        return await _fake_relay(200, body)(
            request, upstream_url, *args, **kwargs)

    relay = AsyncMock(side_effect=send)
    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["output"]["email"] == "synthetic@example.com"
    if unavailable:
        assert result["_treg"]["served_by"] == SEARCH
        assert attempts == {FIND: 1, SEARCH: 1}
        charged = int(response.headers["X-Treg-Cost-Micro"])
        assert charged > 0 and before - await _balance(clients) == charged
        assert len(sent) == 1 and "influencers.club" in sent[0]
    else:
        assert result["_treg"]["served_by"] == EP
        assert result["_treg"]["tried"][0]["outcome"] == "skipped"
        assert result["_treg"]["tried"][0]["charged_micro"] == 0
        assert attempts == {FIND: 2, SEARCH: 1, EP: 1}
        assert before - await _balance(clients) == 1000
        assert len(sent) == 1 and "influencers.club" not in sent[0]
    holds, entries = await _money()
    assert not holds
    settled = {entry.call_id for entry in entries if entry.kind == "settle"}
    assert settled == {response.headers["X-Treg-Call-Id"] + (":r0" if unavailable else ":r1")}


@pytest.mark.parametrize("allow_fail", [False, True])
async def test_hub_quota_refusal_has_zero_child_cost_but_preserves_other_step_billing(
    clients, monkeypatch, platform_on, hub_on, allow_fail,
):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub,influencersclub")
    monkeypatch.setenv("TREG_PLATFORM_KEY_INFLUENCERSCLUB", "SYNTHETIC-KEY")
    get_settings.cache_clear()
    # The refs make each step wait for its predecessor, so the billing assertion is deterministic.
    manifest = _steps_manifest(
        name="quota-boundary", uses=[EP, SEARCH],
        steps=[
            {"name": "paid", "call": EP, "input": {"aweme_id": "$input.domain"}},
            {"name": "blocked", "call": SEARCH, "allow_fail": allow_fail,
             "input": {**_body(), "nlp_search": "$paid.data"}},
            {"name": "later", "call": EP,
             "input": {"aweme_id": "later", "cursor": "$blocked.total"}},
        ], output={"rows": "$later.data", "prior": "$paid.data"},
    )
    seen = []
    blocked = False

    async def relay(request, upstream_url, *args, **kwargs):
        if before_outbound := kwargs.pop("before_outbound", None):
            await before_outbound()
        if "influencers.club" in upstream_url:
            assert not blocked, "quota-protected provider must never receive the buyer's request"
            body = b'{"accounts":[{"user_id":"synthetic"}],"total":1,"credits_cost":0.01}'
        else:
            seen.append(upstream_url)
            body = b'{"data":"synthetic"}'
        return await _fake_relay(200, body)(request, upstream_url, *args, **kwargs)

    monkeypatch.setattr(call_service, "relay", relay)
    published = await clients.post("/hub/tools", json={
        "manifest": manifest, "check": {"inputs": {"domain": "synthetic"}, "fields": ["rows"]},
        "readme": "Local synthetic quota boundary check.",
    })
    assert published.status_code == 201, published.text
    assert published.json()["status"] == "live", published.text
    tool_id = published.json()["tool_id"]
    buyer = await funded_user(clients, "quota-boundary-buyer@example.com")
    headers = {"X-Treg-Token": buyer["token"]}

    async def buyer_balance():
        return (await clients.get(f"/orgs/{buyer['org_id']}/balance", headers=headers)).json()["balance_micro"]

    buyer_before, maker_before = await buyer_balance(), await _balance(clients)
    attempts = _block(monkeypatch, buyer["org_id"])
    blocked = True
    seen.clear()
    response = await clients.post(f"/call/{tool_id}", json={"domain": "synthetic"}, headers=headers)
    assert response.status_code == (200 if allow_fail else 424), response.text
    result = response.json() if allow_fail else response.json()["detail"]
    step = next(entry for entry in result["trace"] if entry["name"] == "blocked")
    assert step["status"] == 429 and step["cost_micro"] == 0
    assert step["outcome"] == ("failed_allowed" if allow_fail else "failed")
    assert attempts == {EP: 2 if allow_fail else 1, SEARCH: 1}
    assert len(seen) == (2 if allow_fail else 1)
    assert buyer_before - await buyer_balance() == (12_000 if allow_fail else 1000)
    assert await _balance(clients) - maker_before == (10_000 if allow_fail else 0)
    if allow_fail:
        assert result["output"]["rows"] == "synthetic"
        assert result["usage"]["steps_micro"] == 2000
        assert result["usage"]["price_micro"] == 10_000
    else:
        assert result["error"] == "hub_step_failed"
        assert result["charged_micro"] == 1000 and result["price_micro"] == 0
        assert not any(entry["name"] == "later" for entry in result["trace"])
    holds, entries = await _money()
    assert not holds
    blocked_entries = [entry for entry in entries if entry.call_id == result["run_id"] + ":s1"]
    assert not [entry for entry in blocked_entries if entry.kind == "settle"]
    assert sum(entry.amount_micro for entry in blocked_entries) == 0
