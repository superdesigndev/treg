"""RichAPI person and company enrich: platform price plus the free usage probe."""
from __future__ import annotations

import httpx
from httpx import AsyncClient

from treg.api import app
from treg.domain.catalog import store as cs


def test_every_richapi_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "richapi"]
    assert [e["id"] for e in eps] == ["richapi.people.enrich", "richapi.companies.enrich"]
    for ep in eps:
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "richapi")["usd"] > 0, ep["id"]


async def test_richapi_key_uses_free_usage_probe(clients, monkeypatch):
    def probe(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/usage"
        assert request.headers["x-api-key"] == "own-key"
        return httpx.Response(200, json={"credits_remaining": 1})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token", json={"provider": "richapi", "token": "own-key"},
        )
    assert response.status_code == 200, response.text


async def test_richapi_rejects_a_key_its_usage_route_refuses(clients, monkeypatch):
    """The live answer to a bogus key, 2026-10-15: 401 {"error":"Missing API key identity"}."""
    async with AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            401, json={"error": "Missing API key identity"}))) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token", json={"provider": "richapi", "token": "bogus-key"},
        )
    assert response.status_code == 422, response.text
    assert "rejected" in response.text
