"""Financial Datasets catalog surface, routing, metering and BYOK precedence."""

from __future__ import annotations


import pytest

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store

from test_marketplace_call import _balance, _fake_relay, _telemetry


@pytest.fixture
def financialdatasets_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_FINANCIALDATASETS", "PLATFORM-FD-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "financialdatasets")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_anonymous_discovery_uses_no_provider_key_and_does_not_change_the_balance(
    clients, financialdatasets_on, monkeypatch,
):
    seen = {}
    fake = _fake_relay(200, b'{"tickers":["AAPL"]}')

    async def capture(*args, **kwargs):
        seen["bindings"] = args[2].bindings
        seen["secrets"] = args[3]
        return await fake(*args, **kwargs)

    monkeypatch.setattr(call_service, "relay", capture)
    before = await _balance(clients)
    response = await clients.get("/call/financialdatasets.prices.tickers")
    assert response.status_code == 200, response.text
    assert "X-Treg-Cost-Micro" not in response.headers
    assert await _balance(clients) == before
    assert seen == {"bindings": [], "secrets": {}}
    assert (await _telemetry(clients))["credential_tier"] == "anonymous"

    access = (await clients.get(
        "/catalog/endpoints/financialdatasets.prices.tickers/access"
    )).json()
    assert access["tier"] == "anonymous" and access["metered"] is False
    detail = (await clients.get(
        "/catalog/endpoints/financialdatasets.prices.tickers"
    )).json()
    assert detail["endpoint"]["platform_auth"] == "anonymous"
    assert any("no provider key required" in hint for hint in detail["hints"])


async def test_anonymous_discovery_needs_the_provider_allowlist_but_not_a_platform_key(
    clients, monkeypatch,
):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "financialdatasets")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FINANCIALDATASETS", "")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"tickers":["AAPL"]}'))
        response = await clients.get("/call/financialdatasets.prices.tickers")
        assert response.status_code == 200
        assert (await _telemetry(clients))["credential_tier"] == "anonymous"
    finally:
        get_settings.cache_clear()


async def test_own_financialdatasets_key_still_wins_for_an_anonymous_capable_tool(
    clients, financialdatasets_on, monkeypatch,
):
    await clients.post("/secrets", json={"name": "financialdatasets", "value": "OWN-FD-KEY"})
    seen = {}
    fake = _fake_relay(200, b'{"tickers":["AAPL"]}')

    async def capture(*args, **kwargs):
        seen["bindings"] = args[2].bindings
        seen["secrets"] = args[3]
        return await fake(*args, **kwargs)

    monkeypatch.setattr(call_service, "relay", capture)
    before = await _balance(clients)
    response = await clients.get("/call/financialdatasets.prices.tickers")
    assert response.status_code == 200
    assert await _balance(clients) == before
    assert seen["bindings"] and seen["secrets"]
    assert (await _telemetry(clients))["credential_tier"] == "credential"


async def test_existing_router_can_plan_a_generic_anonymous_child(
    clients, monkeypatch,
):
    cat = catalog_store.load()
    endpoint = cat.by_id["financialdatasets.prices.snapshot"]
    monkeypatch.setitem(endpoint, "platform_auth", "anonymous")
    monkeypatch.setitem(endpoint, "cost", {
        "type": "free", "value": 0, "currency": "USD", "unit": "call",
    })
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "financialdatasets")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FINANCIALDATASETS", "")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(
            call_service, "relay",
            _fake_relay(200, b'{"snapshot":{"ticker":"AAPL","price":334.48}}'),
        )
        response = await clients.post(
            "/call/treg.stocks.quote.live", json={"symbol": "AAPL"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["_treg"]["tier"] == "anonymous"
        assert await _balance(clients) == 1_000_000
        access = (await clients.get(
            "/catalog/endpoints/treg.stocks.quote.live/access"
        )).json()
        assert access["tier"] == "routed"
        assert "verified public upstream route, no provider key" in access["detail"]
        assert "treg's financialdatasets key" not in access["detail"]
    finally:
        get_settings.cache_clear()
