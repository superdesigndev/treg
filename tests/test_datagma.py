from __future__ import annotations

import json

import httpx
import pytest

from treg import api as A
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.domain.capacity import collectors, policy


def _mk(unit_micro=22_580):
    return MarketplaceCall(
        tool=None, upstream="https://gateway.datagma.net/api/ingress/v8/findEmail",
        consumed=set(), provider="datagma", endpoint_id="datagma.people.email.find",
        tier="platform", estimate_micro=unit_micro, cost_type="per_success",
        unit_micro=unit_micro, request_data={},
    )


@pytest.mark.parametrize("raw,expected", [
    ("0", 0), ("1", 22_580), (30, 677_400), ("1.5", 33_870),
    (-1, None), (True, None), ("bad", None), (None, None),
])
def test_datagma_settles_from_reported_credit_burn(raw, expected):
    assert call_settle._observed_cost_micro(
        _mk(), json.dumps({"creditBurn": raw}).encode()) == expected


async def test_datagma_internal_balance_exposes_only_credit_count(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAGMA", "private-test-key")
    collectors.get_settings.cache_clear()

    def reply(request):
        assert request.url.path == "/api/ingress/v1/mine"
        assert request.url.params["apiId"] == "private-test-key"
        return httpx.Response(200, json={
            "currentCredit": "3002", "email": "owner@example.test", "plan": "private",
        })

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            row = await collectors.provider_balance("datagma", client)
        assert row == {"provider": "datagma", "value": 3002, "unit": "credits",
                       "note": "Prepaid balance; replenished manually"}
        assert "owner" not in str(row) and "plan" not in str(row)
        capacity = policy.default_policy("datagma", has_key=True)
        assert capacity.funding_mode == "manual"
        assert capacity.rate_limit == {"limit": 10, "window_s": 1, "source": "docs"}
    finally:
        collectors.get_settings.cache_clear()


async def test_datagma_balance_failure_does_not_expose_query_key(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAGMA", "private-test-key")
    collectors.get_settings.cache_clear()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "bad"}, request=request))) as client:
            row = await collectors.provider_balance("datagma", client)
        assert row["value"] is None
        assert "private-test-key" not in str(row)
    finally:
        collectors.get_settings.cache_clear()


async def test_datagma_connection_probe_is_internal_and_generic(clients, monkeypatch):
    def probe(request):
        assert request.url.path == "/api/ingress/v1/mine"
        if request.url.params["apiId"] == "bad":
            return httpx.Response(401, json={"account": "must-not-leak"})
        return httpx.Response(200, json={"currentCredit": "3002", "email": "must-not-leak"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "datagma", "token": "bad"})
        assert bad.status_code == 422 and "must-not-leak" not in bad.text
        good = await clients.post("/connections/token", json={"provider": "datagma", "token": "own"})
        assert good.status_code == 200 and "currentCredit" not in good.text and "must-not-leak" not in good.text
