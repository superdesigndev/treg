"""AnyAPI forwards the caller's ceiling as max_cost_usd, and a refusal stays uncharged.

The header used to be only a pre-reservation check. AnyAPI settles `costUsd`, which a failover
source can set above both the catalog price and the header. The provider's own query parameter
refuses that source before it runs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A
from treg.application.call import service as call_service
from treg.application.call.service import _format_ceiling_usd
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.catalog import store as catalog_store

from test_marketplace_call import _balance, _dropleads_response, _entries, _telemetry

EP = "anyapi.linkedin.search.posts"
HDR = "X-Treg-Route-Max-Cost"
BODY = {"query": "jobs", "datePosted": "last-week"}


@pytest.fixture
def anyapi_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ANYAPI", "PLATFORM-ANYAPI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "anyapi")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_ceiling_usd_has_no_exponent():
    assert _format_ceiling_usd(3_000) == "0.003"
    assert _format_ceiling_usd(10_000) == "0.01"
    assert _format_ceiling_usd(1_000_000) == "1"
    assert "e" not in _format_ceiling_usd(1).lower()


def test_a_row_ceiling_param_overrides_the_provider_rule(monkeypatch):
    cat = catalog_store.Catalog(
        by_id={
            "demo.one": {"provider": "demo", "cost": {"type": "per_call", "value": 1}},
            "demo.two": {"provider": "demo", "cost": {
                "type": "per_call", "value": 1,
                "ceiling_param": {"in": "query", "name": "cap", "unit": "usd"},
            }},
            "other.one": {"provider": "other", "cost": {"type": "per_call", "value": 1}},
        },
        ceiling_params={"demo": {"in": "query", "name": "max_cost_usd", "unit": "usd"}},
    )
    monkeypatch.setattr(catalog_store, "load", lambda **kwargs: cat)

    def call(endpoint_id, query=()):
        mk = SimpleNamespace(endpoint_id=endpoint_id, provider="demo", max_cost_micro=3_000)
        return call_service._ceiling_query(mk, query)

    assert call("demo.one") == (("max_cost_usd", "0.003"),)
    assert call("demo.two") == (("cap", "0.003"),)
    assert call("demo.one", (("max_cost_usd", "0.001"),)) == ()
    assert call("other.one") == ()


async def test_direct_ceiling_is_forwarded_unless_the_caller_already_set_it(
    clients: AsyncClient, anyapi_on, fake_getaddrinfo, monkeypatch,
):
    fake_getaddrinfo({"api.getanyapi.com": ["1.1.1.1"]})
    plain = await clients.post(f"/call/{EP}", json=BODY)
    assert plain.status_code == 200, plain.text
    assert "max_cost_usd" not in plain.json()["query"]
    plain_hash = (await _telemetry(clients))["params_hash"]

    capped = await clients.post(f"/call/{EP}", json=BODY, headers={HDR: "0.003"})
    assert capped.status_code == 200, capped.text
    assert capped.json()["query"]["max_cost_usd"] == "0.003"
    assert [pair for pair in capped.json()["query_multi"] if pair[0] == "max_cost_usd"] == [
        ["max_cost_usd", "0.003"]]
    assert (await _telemetry(clients))["params_hash"] == plain_hash

    explicit = await clients.post(
        f"/call/{EP}?max_cost_usd=0.001", json=BODY, headers={HDR: "0.003"})
    assert explicit.status_code == 200, explicit.text
    assert [pair for pair in explicit.json()["query_multi"] if pair[0] == "max_cost_usd"] == [
        ["max_cost_usd", "0.001"]]
    assert (await _telemetry(clients))["params_hash"] != plain_hash

    monkeypatch.setattr(get_settings(), "platform_margin", 0.25)
    margined = await clients.post(f"/call/{EP}", json=BODY, headers={HDR: "0.003"})
    assert margined.status_code == 200, margined.text
    assert margined.json()["query"]["max_cost_usd"] == "0.0024"
    assert ledger.with_margin(2_400) <= 3_000 < ledger.with_margin(2_401)


async def test_max_cost_exceeded_releases_the_hold(
    clients: AsyncClient, anyapi_on, fake_getaddrinfo,
):
    fake_getaddrinfo({"api.getanyapi.com": ["1.1.1.1"]})
    seen: list[list[tuple[str, str]]] = []

    def serve(request: httpx.Request):
        seen.append(list(request.url.params.multi_items()))
        return _dropleads_response(400, {
            "code": "max_cost_exceeded",
            "error": ("max_cost_exceeded: no source serves this request within $0.003; "
                      "the cheapest is $0.008 per request (no charge)"),
        })

    before = await _balance(clients)
    original = A.app.state.http
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
            A.app.state.http = upstream
            response = await clients.post(f"/call/{EP}", json=BODY, headers={HDR: "0.003"})
    finally:
        A.app.state.http = original
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "max_cost_exceeded"
    assert response.headers["x-treg-cost-micro"] == "0"
    assert seen == [[("max_cost_usd", "0.003")]]
    assert await _balance(clients) == before
    kinds = [entry["kind"] for entry in await _entries(clients)]
    assert "release" in kinds and "settle" not in kinds
    assert (await _telemetry(clients))["cost_charged_micro"] == 0


def _relay_by_provider(answers: dict, seen: list):
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        provider = next((name for name in answers if name != "*" and name in upstream_url), "*")
        status, doc = answers[provider].pop(0)
        seen.append((provider, dict(request.query_items)))
        payload = json.dumps(doc).encode()

        async def _stream():
            yield payload

        async def _close():
            return None

        return UpstreamResponse(status, ((b"content-type", b"application/json"),), _stream(), _close)

    return _relay


async def test_routed_anyapi_child_receives_the_remaining_ceiling(clients, monkeypatch, anyapi_on):
    """A routed child inherits the caller's ceiling, including the route default when unset."""
    seen: list = []
    hit = {"costUsd": 0.0009, "output": {"data": {"results": [{"title": "t", "link": "https://example.com"}]}}}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"anyapi": [(200, hit), (200, hit)]}, seen))
    capped = await clients.post(
        "/call/treg.google.serp.organic", json={"q": "pizza"},
        headers={HDR: "0.01", "X-Treg-Route-Prefer": "anyapi"})
    assert capped.status_code == 200, capped.text
    assert seen[0] == ("anyapi", {"max_cost_usd": "0.01"})
    seen.clear()
    plain = await clients.post(
        "/call/treg.google.serp.organic", json={"q": "pizza"},
        headers={"X-Treg-Route-Prefer": "anyapi"})
    assert plain.status_code == 200, plain.text
    assert seen[0] == ("anyapi", {"max_cost_usd": "1"})


async def test_routed_max_cost_exceeded_is_a_failed_attempt(clients, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ANYAPI", "PLATFORM-ANYAPI")
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-CRAWL4AI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "anyapi,crawl4ai")
    get_settings.cache_clear()
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "anyapi": [(400, {
            "code": "max_cost_exceeded",
            "error": "max_cost_exceeded: no source serves this request within $0.01; the cheapest is $0.02 per request (no charge)",
        })],
        "crawl4ai": [(200, {"results": [{"title": "t", "url": "https://example.com", "snippet": "s"}]})],
    }, seen))
    before = await _balance(clients)
    response = await clients.post(
        "/call/treg.google.serp.organic", json={"q": "pizza"},
        headers={HDR: "0.01", "X-Treg-Route-Prefer": "anyapi"})
    assert response.status_code == 200, response.text
    body = response.json()
    tried = body["_treg"]["tried"]
    assert tried[0]["endpoint_id"] == "anyapi.google.serp.organic"
    assert tried[0]["outcome"] == "error" and tried[0]["status"] == 400
    assert tried[0]["charged_micro"] == 0
    assert tried[1]["provider"] == "crawl4ai" and tried[1]["outcome"] == "hit"
    assert seen[0] == ("anyapi", {"max_cost_usd": "0.01"})
    assert "max_cost_usd" not in seen[1][1]
    assert body["_treg"]["charged_micro"] == tried[1]["charged_micro"]
    assert await _balance(clients) == before - tried[1]["charged_micro"]
    get_settings.cache_clear()
