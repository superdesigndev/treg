from __future__ import annotations

import json

import pytest

from treg.application.call import service as call_service
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings


def _mk(*, endpoint_id="scrubby.people.email.verify", unit_micro=8_000):
    return MarketplaceCall(
        tool=None, upstream="https://api.scrubby.io/validate_email", consumed=set(),
        provider="scrubby", endpoint_id=endpoint_id, tier="platform",
        estimate_micro=unit_micro, cost_type="per_success", unit_micro=unit_micro,
        request_data={},
    )


def _relay(status: int, doc: dict):
    async def relay(*args, **kwargs):
        async def stream():
            yield json.dumps(doc).encode()

        async def close():
            return None

        return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)
    return relay


async def _balance(clients):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


@pytest.mark.parametrize("credits,expected", [(0, 0), (1, 8_000), (3, 24_000), (-1, None), (True, None), (1.5, None), (None, None)])
def test_scrubby_settles_from_reported_credits(credits, expected):
    body = json.dumps({"result": "Valid", "credits_used": credits}).encode()
    assert call_settle._observed_cost_micro(_mk(), body) == expected


async def test_scrubby_platform_settles_exact_usage_and_byok_wins(clients, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_SCRUBBY", "PLATFORM-SCRUBBY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "scrubby")
    get_settings.cache_clear()
    monkeypatch.setattr(call_service, "relay", _relay(200, {
        "email": "a@example.com", "result": "Invalid", "status": "HARD_BOUNCE",
        "quick_status": "HARD_BOUNCE", "credits_used": 1, "remaining_credits": 9,
    }))
    before = await _balance(clients)
    response = await clients.post("/call/scrubby.people.email.verify", json={"email": "a@example.com"})
    assert response.status_code == 200, response.text
    assert response.headers["x-treg-cost-micro"] == "8000"
    assert await _balance(clients) == before - 8_000

    await clients.post("/secrets", json={"name": "scrubby", "value": "OWN-SCRUBBY"})
    before_byok = await _balance(clients)
    response = await clients.post("/call/scrubby.people.email.verify", json={"email": "a@example.com"})
    assert response.status_code == 200
    assert "x-treg-cost-micro" not in response.headers
    assert await _balance(clients) == before_byok
    get_settings.cache_clear()


