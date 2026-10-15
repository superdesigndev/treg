"""LOPJLB listing: header key, free public routes, paid depth stays bring-your-own-key."""

import httpx
from httpx import AsyncClient

from treg.api import app
from treg.domain.catalog import store as catalog_store
from treg.oauth_providers import get


def test_lopjlb_connects_with_a_header_key():
    provider = get("lopjlb")
    assert provider is not None
    assert provider.auth_kind == "key"
    assert provider.uses_pasted_secret is True
    assert provider.token_header == "X-LOPJLB-API-Key"
    assert provider.token_format == "{secret}"
    assert provider.base_url == "https://www.lopjlb.com/bff/api"
    # Public routes ignore a bad key (200). Peers is the route that rejects one.
    assert provider.probe_path == "/bigfive/peers/AAPL"


def test_lopjlb_public_routes_are_free_and_paid_depth_is_not_platform_served():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "lopjlb"]
    assert 8 <= len(rows) <= 15
    blocked = {"lopjlb.signals.scorecard", "lopjlb.peers"}
    assert blocked <= {ep["id"] for ep in rows}
    for ep in rows:
        assert ep["cost"]["type"] == "free"
        assert ep["cost"]["value"] == 0
        if ep["id"] in blocked:
            assert ep["platform_blocked"]
            assert catalog.platform_eligible(ep) is False
        else:
            assert not ep["platform_blocked"]
            assert catalog.platform_eligible(ep) is True


async def test_lopjlb_rejects_the_observed_bogus_key_body(clients, monkeypatch):
    """Live 2026-10-11: GET /bigfive/peers/AAPL with a garbage key returned 401 {"error":"invalid_api_key"}."""
    seen = {}

    def probe(request):
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get("x-lopjlb-api-key")
        return httpx.Response(401, json={"error": "invalid_api_key"})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token", json={"provider": "lopjlb", "token": "not-a-real-key"},
        )
    assert response.status_code == 422, response.text
    assert "rejected" in response.text
    assert seen["header"] == "not-a-real-key"
    assert seen["url"].endswith("/bff/api/bigfive/peers/AAPL")


async def test_lopjlb_accepts_a_key_when_the_peers_probe_returns_200(clients, monkeypatch):
    async with AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"symbol": "AAPL", "peers": []}))) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token", json={"provider": "lopjlb", "token": "own-key"},
        )
    assert response.status_code == 200, response.text
