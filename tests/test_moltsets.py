"""MoltSets charging, capacity, and shared-key safety boundaries."""
import json

import httpx
import pytest

from treg.application.call import service
from treg.config import get_settings
from treg.domain.capacity import collectors
from treg.domain.capacity.policy import default_policy
from test_marketplace_call import _balance, _entries, _fake_relay, platform_on


@pytest.fixture
def moltsets_on(monkeypatch, platform_on):
    monkeypatch.setenv("TREG_PLATFORM_KEY_MOLTSETS", "PLATFORM-MOLTSETS")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "moltsets")
    get_settings.cache_clear()


async def test_platform_success_charges_and_miss_or_error_does_not(clients, moltsets_on, monkeypatch):
    before = await _balance(clients)
    cases = [
        (200, {"results": {"email": "alex@example.com"}, "status": "ok"}, 10_000),
        (200, {"results": {"email": None}, "status": "not_found"}, 0),
        (422, {"error": {"code": "validation_error"}}, 0),
        (500, {"error": {"code": "upstream_error"}}, 0),
    ]
    spent = 0
    for status, body, charge in cases:
        raw = json.dumps(body).encode()
        monkeypatch.setattr(service, "relay", _fake_relay(status, raw))
        response = await clients.post("/call/moltsets.people.email.find.name", json={
            "name": "Alex Example", "company_domain": "example.com",
        })
        assert response.status_code == status and response.content == raw
        spent += charge
        assert await _balance(clients) == before - spent
    money = [e["kind"] for e in await _entries(clients) if e["kind"] in ("reserve", "settle", "release")]
    assert money.count("reserve") == 4
    assert money.count("settle") + money.count("release") == 4


@pytest.mark.parametrize("endpoint,body", [
    ("people.search", {"query": "engineer", "limit": 2}),
    ("companies.search", {"query": "example", "limit": 2}),
    ("linkedin.profile.search", {"name": "Alex Example"}),
])
async def test_variable_search_tools_are_not_shared_key_offers(
        clients, moltsets_on, monkeypatch, endpoint, body):
    async def forbidden(*args, **kwargs):
        raise AssertionError("platform guard must precede relay")

    monkeypatch.setattr(service, "relay", forbidden)
    response = await clients.post("/call/moltsets." + endpoint, json=body)
    assert response.status_code == 404, response.text
    assert not [e for e in await _entries(clients) if e["kind"] in ("reserve", "settle", "release")]


async def test_byok_can_call_blocked_search_tools_without_treg_meter(clients, moltsets_on):
    await clients.post("/secrets", json={"name": "moltsets", "value": "OWN-MOLTSETS"})
    before = await _balance(clients)
    bodies = {
        "people.search": {"query": "engineer", "limit": 2},
    }
    for endpoint, body in bodies.items():
        response = await clients.post(
            "/call/moltsets." + endpoint, json=body, headers={"User-Agent": ""}
        )
        assert response.status_code == 200, response.text
        echoed = response.json()
        assert echoed["auth"] == "Bearer OWN-MOLTSETS"
        assert echoed["headers"]["user-agent"] == "treg/1.0 (+https://treg.to)"
        assert json.loads(echoed["body"]) == body
    assert await _balance(clients) == before
    assert not [e for e in await _entries(clients) if e["kind"] in ("reserve", "settle", "release")]


@pytest.mark.parametrize("five_hour,weekly,token_balance,expected", [
    (997, 4997, -1, 997),
    (0, 4997, -1, 0),
    (None, None, 42, None),
    (None, None, -1, None),
])
async def test_capacity_probe_and_policy(five_hour, weekly, token_balance, expected):
    account = {
        "status": "active", "plan": "subscription_27", "token_balance": token_balance,
        "fair_use": {
            "enrich": {"records": {"5h": {"remaining": five_hour}, "1w": {"remaining": weekly}},
                       "requests": {"5h": {"remaining": 4999}}},
            "search": {"records": {"5h": {"remaining": 499}, "1w": {"remaining": 2499}},
                       "requests": {"5h": {"remaining": 2499}}},
        },
    }
    def probe(request):
        assert request.method == "POST" and json.loads(request.content) == {}
        assert request.headers["authorization"] == "Bearer test"
        assert request.headers["user-agent"] == "treg/1.0 (+https://treg.to)"
        return httpx.Response(200, json={"results": account, "status": "ok"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._moltsets(client, "test")
    assert row["value"] == expected and row["unit"] == "enrichment records"
    assert "search records 499/5h" in row["note"]
    assert "never substituted for enrichment capacity" in row["note"]
    policy = default_policy("moltsets", has_key=True)
    assert policy.capacity_type == "rolling_quota"
    assert policy.funding_mode == "subscription"
    assert policy.rate_limit == {"limit": 10, "window_s": 1, "source": "policy"}


