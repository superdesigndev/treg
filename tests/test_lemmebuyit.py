"""LemMeBuyIt REST connections validate keys without requiring Walmart access."""

import httpx
import pytest

from treg.api import app


async def test_connect_uses_authenticated_retailer_discovery(clients, monkeypatch):
    def probe(request):
        assert str(request.url) == "https://api.lemmebuyit.com/v1/retailers"
        assert request.method == "GET"
        assert request.headers["x-api-key"] == "own-key"
        assert "authorization" not in request.headers
        # A REST key may have access to a retailer other than Walmart.
        return httpx.Response(200, json=[{"id": "target", "name": "Target"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token", json={"provider": "lemmebuyit", "token": "own-key"},
        )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("status, error", [
    (401, "Invalid API key"),
    (403, "This API key is restricted to MCP access"),
])
async def test_connect_rejects_invalid_or_mcp_only_keys(clients, monkeypatch, status, error):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"error": error}),
    )) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token", json={"provider": "lemmebuyit", "token": "rejected-key"},
        )
    assert response.status_code == 422, response.text
    assert "rejected" in response.text
