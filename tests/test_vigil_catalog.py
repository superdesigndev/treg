"""Vigil catalog surface, routing, metering and BYOK precedence.

Vigil sells four tools (three SEC and congressional disclosure datasets plus
a free ticker-CIK probe) through treg's normal platform access (treg bills its
own users from its own Vigil key), with an optional org BYOK key that — when
present — takes precedence and leaves the treg balance untouched. The
ticker-CIK probe is free but still keyed: it never serves anonymously.

Every test below exercises the real resolver/relay/ledger path with a
deterministic synthetic upstream (httpx.MockTransport): no live provider,
no credentials, no source-text assertions.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store

from test_marketplace_call import _balance, _telemetry

CONGRESSIONAL = "vigil.stocks.congressional-trades"   # GET /v1/congressional-trades, $0.02
INSIDER = "vigil.stocks.insider-trades"                # GET /v1/insider-trades, $0.01
INSIDER_PARAMS = {"ticker": "NVDA", "side": "sell", "limit": 1}
INTENT = "vigil.stocks.insider-intent"                 # GET /v1/insider-intent, $0.02
PROBE = "vigil.stocks.reference.ticker-cik"           # GET /v1/reference/ticker-cik, free but keyed

PLATFORM_KEY = "PLATFORM-VIGIL-KEY"  # synthetic, never a real credential
OWN_KEY = "OWN-VIGIL-KEY"            # synthetic org BYOK key


@pytest.fixture
def vigil_on(monkeypatch):
    """Tier 4 the way a deploy turns it on: platform key set AND provider allow-listed."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_VIGIL", PLATFORM_KEY)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "vigil")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _JSONStream(httpx.AsyncByteStream):
    def __init__(self, doc: dict):
        import json

        self.body = json.dumps(doc).encode()

    async def __aiter__(self):
        yield self.body


def _vendor(body: dict, status: int = 200, seen: list | None = None):
    """Synthetic Vigil app: records the request, answers with a fixed JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(
            status,
            headers={"content-type": "application/json"},
            stream=_JSONStream(body),
        )

    return handler


async def _platform_vendor(clients: AsyncClient, monkeypatch, seen: list, body: dict,
                           status: int = 200):
    await A.app.state.http.aclose()
    A.app.state.http = AsyncClient(
        transport=httpx.MockTransport(_vendor(body, status, seen)))
    return seen


async def test_platform_insider_call_bills_per_success_on_the_platform_key(
    clients, vigil_on, monkeypatch,
):
    """No org key: treg's own key pays, the caller is metered $0.01 (10000 micro-USD)."""
    seen: list = []
    await _platform_vendor(
        clients, monkeypatch, seen,
        {"items": [{"insider_name": "Sample Insider", "ticker": "NVDA"}],
         "coverage": {}, "resolution": {}},
    )
    before = await _balance(clients)
    response = await clients.get(f"/call/{INSIDER}", params=INSIDER_PARAMS)
    assert response.status_code == 200, response.text
    assert response.headers["X-Treg-Cost-Micro"] == "10000"
    assert await _balance(clients) == before - 10_000
    (request,) = seen
    assert request.url.path == "/v1/insider-trades"
    assert request.url.params["ticker"] == "NVDA"
    assert request.url.params["side"] == "sell"
    assert request.url.params["limit"] == "1"
    assert request.headers["x-vigil-key"] == PLATFORM_KEY


async def test_platform_congressional_call_bills_per_success_on_the_platform_key(
    clients, vigil_on, monkeypatch,
):
    """No org key: treg's own key pays, the caller is metered $0.02 (20000 micro-USD)."""
    seen: list = []
    await _platform_vendor(
        clients, monkeypatch, seen,
        {"items": [{"member_name": "Sample Member", "ticker": "NVDA"}],
         "coverage": {}, "resolution": {}},
    )
    before = await _balance(clients)
    response = await clients.get(f"/call/{CONGRESSIONAL}", params={"ticker": "NVDA", "limit": 1})
    assert response.status_code == 200, response.text
    assert response.headers["X-Treg-Cost-Micro"] == "20000"
    assert await _balance(clients) == before - 20_000
    (request,) = seen
    assert request.url.path == "/v1/congressional-trades"
    assert request.url.params["ticker"] == "NVDA"
    assert request.url.params["limit"] == "1"
    assert request.headers["x-vigil-key"] == PLATFORM_KEY


async def test_paid_empty_200_still_bills_full_rate(clients, vigil_on, monkeypatch):
    """A paid HTTP 200 with items:[] is a billed success, not a generic miss."""
    seen: list = []
    await _platform_vendor(clients, monkeypatch, seen, {"items": []})
    before = await _balance(clients)
    response = await clients.get(f"/call/{INSIDER}", params={"ticker": "NVDA", "limit": 1})
    assert response.status_code == 200, response.text
    assert response.headers["X-Treg-Cost-Micro"] == "10000"
    assert await _balance(clients) == before - 10_000
    assert seen[0].headers["x-vigil-key"] == PLATFORM_KEY


async def test_intent_call_bills_two_cents(clients, vigil_on, monkeypatch):
    seen: list = []
    await _platform_vendor(clients, monkeypatch, seen, {"items": []})
    before = await _balance(clients)
    response = await clients.get(f"/call/{INTENT}", params={"ticker": "NVDA", "limit": 1})
    assert response.status_code == 200, response.text
    assert response.headers["X-Treg-Cost-Micro"] == "20000"
    assert await _balance(clients) == before - 20_000


async def test_org_byok_wins_and_leaves_the_treg_balance_alone(
    clients, vigil_on, monkeypatch,
):
    """A connected org key takes precedence: raw X-Vigil-Key auth, zero treg charge."""
    await clients.post("/secrets", json={"name": "vigil", "value": OWN_KEY})
    seen: list = []
    await _platform_vendor(
        clients, monkeypatch, seen, {"items": [], "coverage": {}, "resolution": {}})
    before = await _balance(clients)
    response = await clients.get(f"/call/{INSIDER}", params=INSIDER_PARAMS)
    assert response.status_code == 200, response.text
    assert "X-Treg-Cost-Micro" not in response.headers
    assert await _balance(clients) == before
    assert seen[0].headers["x-vigil-key"] == OWN_KEY
    assert (await _telemetry(clients))["credential_tier"] == "credential"


async def test_bad_byok_is_rejected_with_no_platform_fallback(
    clients, vigil_on, monkeypatch,
):
    """A revoked org key fails upstream: no silent retry on treg's key, no debit."""
    await clients.post("/secrets", json={"name": "vigil", "value": "BAD-VIGIL-KEY"})
    seen: list = []
    await _platform_vendor(
        clients, monkeypatch, seen, {"error": "invalid_api_key"}, status=401)
    before = await _balance(clients)
    response = await clients.get(f"/call/{INSIDER}", params=INSIDER_PARAMS)
    assert response.status_code != 200, response.text
    assert await _balance(clients) == before
    assert len(seen) == 1, "must not retry a rejected BYOK key on the platform key"
    assert seen[0].headers["x-vigil-key"] == "BAD-VIGIL-KEY"


async def test_free_keyed_probe_is_served_on_the_platform_key_without_charge(
    clients, vigil_on, monkeypatch,
):
    """The probe costs $0 but still authenticates: q=NVDA reaches Vigil on treg's key."""
    seen: list = []
    await _platform_vendor(
        clients, monkeypatch, seen,
        {"items": [{"ticker": "NVDA", "cik": "1045810"}]},
    )
    before = await _balance(clients)
    response = await clients.get(f"/call/{PROBE}", params={"q": "NVDA"})
    assert response.status_code == 200, response.text
    assert await _balance(clients) == before
    (request,) = seen
    assert request.url.path == "/v1/reference/ticker-cik"
    assert request.url.params["q"] == "NVDA"
    assert request.headers["x-vigil-key"] == PLATFORM_KEY
    assert (await _telemetry(clients))["credential_tier"] == "platform"


async def test_probe_requires_q_before_any_credential(clients, vigil_on, monkeypatch):
    before = await _balance(clients)
    response = await clients.get(f"/call/{PROBE}")
    assert response.status_code == 400, response.text
    assert await _balance(clients) == before


async def test_upstream_errors_leave_the_balance_alone(clients, vigil_on, monkeypatch):
    """Provider 4xx/5xx are unbilled: no usage row spend, balance untouched."""
    seen: list = []
    await _platform_vendor(clients, monkeypatch, seen, {"error": "boom"}, status=500)
    before = await _balance(clients)
    response = await clients.get(f"/call/{INSIDER}", params=INSIDER_PARAMS)
    assert response.status_code == 500, response.text
    assert await _balance(clients) == before


async def test_congressional_upstream_errors_leave_the_balance_alone(
    clients, vigil_on, monkeypatch,
):
    """Provider 4xx/5xx on the congressional tool are unbilled: balance untouched."""
    seen: list = []
    await _platform_vendor(clients, monkeypatch, seen, {"error": "boom"}, status=500)
    before = await _balance(clients)
    response = await clients.get(f"/call/{CONGRESSIONAL}", params={"ticker": "NVDA", "limit": 1})
    assert response.status_code == 500, response.text
    assert await _balance(clients) == before


async def test_platform_absent_is_a_connect_hint_not_a_billable_call(clients, monkeypatch):
    """Without the platform slot/key/allow-list the route refuses; no BYOK fallback is claimed."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_VIGIL", "")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "")
    get_settings.cache_clear()
    try:
        before = await _balance(clients)
        response = await clients.get(
            f"/call/{INSIDER}", params=INSIDER_PARAMS)
        assert response.status_code == 404, response.text
        assert await _balance(clients) == before
    finally:
        get_settings.cache_clear()


async def test_keyed_connect_probe_accepts_a_valid_key_and_rejects_garbage(
    clients, monkeypatch,
):
    """Free keyed connect probe: q=NVDA plus raw X-Vigil-Key; garbage never connects."""
    async def attempt(token: str):
        def probe(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/v1/reference/ticker-cik"
            assert request.url.params["q"] == "NVDA"
            if request.headers["x-vigil-key"] == "GARBAGE-KEY":
                return httpx.Response(401, json={"error": "invalid_api_key"})
            return httpx.Response(200, json={"items": [{"ticker": "NVDA"}]})

        async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
            monkeypatch.setattr(A.app.state, "http", upstream)
            return await clients.post(
                "/connections/token", json={"provider": "vigil", "token": token})

    bad = await attempt("GARBAGE-KEY")
    assert bad.status_code == 422, bad.text
    assert not [c for c in (await clients.get("/connections")).json()
                if c["provider"] == "vigil"]
    good = await attempt("vt_live_unit_test_key")
    assert good.status_code == 200, good.text


def test_every_paid_vigil_route_is_platform_priced():
    """Tier 4: every priced Vigil tool settles in USD on treg's own key."""
    cat = catalog_store.load()
    eps = [e for e in cat.endpoints if e["provider"] == "vigil"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "vigil")["usd"] > 0, ep["id"]


def test_vigil_answers_are_never_cached():
    """Vigil bills per successful call; a stored answer served by treg would be an unbilled resale."""
    import yaml
    from pathlib import Path

    doc = yaml.safe_load((Path(__file__).resolve().parents[1] / "src/treg/catalog/vigil.yaml").read_text())
    assert doc.get("cache") == "forbidden"
