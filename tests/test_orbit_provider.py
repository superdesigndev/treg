"""Orbit's key probe, public catalog coverage, and fail-closed setup contract."""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from treg.api import app
from treg import oauth_providers as providers


def test_orbit_catalog_covers_v3_without_claiming_live_verification():
    """Publish all current operations without treating untested data as verified."""
    source = Path(__file__).parents[1] / "src/treg/catalog/orbit.yaml"
    catalog = yaml.safe_load(source.read_text())
    endpoints = catalog["endpoints"]
    assert len(endpoints) == 16
    assert len({(ep["method"], ep["path"]) for ep in endpoints}) == 16
    for ep in endpoints:
        assert ep["untestable"]
        assert "verified" not in ep
        assert "test_request" not in ep
    by_id = {ep["id"]: ep for ep in endpoints}
    assert by_id["orbit.people.profile.read"]["method"] == "GET"
    assert by_id["orbit.people.enrich"]["method"] == "POST"
    batch = by_id["orbit.people.enrich.batch"]["input"]["body"]["profile_ids"]
    assert (batch["minItems"], batch["maxItems"]) == (1, 20)
    assert by_id["orbit.people.watchers.create"]["scope"] == "own_account"
    assert by_id["orbit.people.webhooks.test"]["cost"]["value"] is None
    assert providers.get("orbit").setup_url == "https://developer.orbitsearch.com/"


@pytest.mark.parametrize("status,payload,accepted", [
    (400, {"status": "failed", "error": {"code": "developer_deep_search_input_required"}}, True),
    (403, {"status": "failure", "error": {"code": "invalid_api_key"}}, False),
    (200, {"status": "failed"}, False),
    (400, {}, False),
    (404, {"status": "failed"}, False),
    (429, {"status": "failed"}, False),
    (500, {"status": "failed"}, False),
    (502, {}, False),
])
async def test_orbit_connect_accepts_only_the_validation_probe(clients, monkeypatch, status, payload, accepted):
    """A gateway response must not mark a customer credential as connected."""
    def upstream(request):
        assert str(request.url) == "https://api.orbitsearch.com/v3/search"
        assert request.method == "POST"
        assert json.loads(request.content) == {}
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        response = await clients.post("/connections/token", json={
            "provider": "orbit", "token": "orbit-test-placeholder",
        })
    assert response.status_code == (200 if accepted else 422), response.text
    if accepted:
        tools = (await clients.get("/tools")).json()
        orbit = next(tool for tool in tools if tool["name"] == "orbit")
        assert orbit["bindings"][0]["format"] == "Bearer {secret}"
        assert orbit["bindings"][0]["name"] == "Authorization"
