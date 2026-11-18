"""Vigil catalog surface, routing, metering and BYOK precedence.

Vigil sells twelve tools (SEC and congressional disclosure datasets, 13F fund
tools, plus a free ticker-CIK probe) through treg's normal platform access
(treg bills its own users from its own Vigil key), with an optional org BYOK key that — when
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

FUNDS_SEARCH = "vigil.funds.search"                    # GET /v1/funds, $0.01
FUND_PROFILE = "vigil.funds.profile"                   # GET /v1/fund, $0.01
FUND_FILINGS = "vigil.funds.filings"                   # GET /v1/fund-filings, $0.01
FUND_HOLDINGS = "vigil.funds.holdings"                 # GET /v1/holdings-13f, $0.01
FUND_CHANGES = "vigil.funds.changes"                   # GET /v1/holding-changes-13f, $0.01
POSITION_HISTORY = "vigil.funds.position-history"      # GET /v1/fund-position-history, $0.01
NEW_POSITIONS = "vigil.funds.new-positions"            # GET /v1/new-13f-positions, $0.01
COMPARE_FILINGS = "vigil.funds.compare-filings"        # GET /v1/compare-13f-filings, $0.01
HOLDERS = "vigil.stocks.institutional-holders"         # GET /v1/institutional-holders, $0.01

FUND_TOOLS = {
    FUNDS_SEARCH: ("/v1/funds", {"q": "BERKSHIRE", "limit": 1}),
    FUND_PROFILE: ("/v1/fund", {"cik": "1067983"}),
    FUND_FILINGS: ("/v1/fund-filings", {"cik": "1067983", "limit": 1}),
    FUND_HOLDINGS: ("/v1/holdings-13f", {"cik": "1067983", "limit": 1}),
    FUND_CHANGES: ("/v1/holding-changes-13f", {"cik": "1067983", "limit": 1}),
    POSITION_HISTORY: ("/v1/fund-position-history",
                       {"cik": "1067983", "ticker": "NVDA", "limit": 1}),
    NEW_POSITIONS: ("/v1/new-13f-positions", {"limit": 1}),
    COMPARE_FILINGS: ("/v1/compare-13f-filings",
                      {"cik": "1067983", "current_accession": "0001193125-26-352200",
                       "baseline_accession": "0001193125-26-226661", "limit": 1}),
    HOLDERS: ("/v1/institutional-holders", {"ticker": "NVDA", "limit": 1}),
}

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


async def test_platform_fund_calls_bill_one_cent_each(clients, vigil_on, monkeypatch):
    """All eight fund tools settle $0.01 (10000 micro-USD) on the platform key."""
    for tool, (path, params) in FUND_TOOLS.items():
        seen: list = []
        await _platform_vendor(clients, monkeypatch, seen, {"items": []})
        before = await _balance(clients)
        response = await clients.get(f"/call/{tool}", params=params)
        assert response.status_code == 200, (tool, response.text)
        assert response.headers["X-Treg-Cost-Micro"] == "10000", tool
        assert await _balance(clients) == before - 10_000, tool
        (request,) = seen
        assert request.url.path == path, tool
        assert request.headers["x-vigil-key"] == PLATFORM_KEY, tool


async def test_fund_paid_empty_200_still_bills(clients, vigil_on, monkeypatch):
    seen: list = []
    await _platform_vendor(clients, monkeypatch, seen, {"items": []})
    before = await _balance(clients)
    response = await clients.get(f"/call/{FUND_HOLDINGS}",
                                 params={"cik": "1067983", "limit": 1})
    assert response.status_code == 200, response.text
    assert response.headers["X-Treg-Cost-Micro"] == "10000"
    assert await _balance(clients) == before - 10_000


async def test_fund_byok_wins_and_leaves_the_treg_balance_alone(
    clients, vigil_on, monkeypatch,
):
    """A connected org key takes precedence on fund tools: zero treg charge."""
    await clients.post("/secrets", json={"name": "vigil", "value": OWN_KEY})
    seen: list = []
    await _platform_vendor(clients, monkeypatch, seen, {"items": []})
    before = await _balance(clients)
    response = await clients.get(f"/call/{COMPARE_FILINGS}",
                                 params={"cik": "1067983",
                                         "current_accession": "0001193125-26-352200",
                                         "baseline_accession": "0001193125-26-226661",
                                         "limit": 1})
    assert response.status_code == 200, response.text
    assert "X-Treg-Cost-Micro" not in response.headers
    assert await _balance(clients) == before
    assert seen[0].headers["x-vigil-key"] == OWN_KEY


async def test_fund_upstream_errors_leave_the_balance_alone(
    clients, vigil_on, monkeypatch,
):
    seen: list = []
    await _platform_vendor(clients, monkeypatch, seen, {"error": "boom"}, status=500)
    before = await _balance(clients)
    response = await clients.get(f"/call/{NEW_POSITIONS}", params={"limit": 1})
    assert response.status_code == 500, response.text
    assert await _balance(clients) == before


async def test_fund_upstream_errors_include_coverage_404(
    clients, vigil_on, monkeypatch,
):
    """A pre-2021 period is an unbilled 404 period_outside_coverage, relayed as 404."""
    seen: list = []
    await _platform_vendor(
        clients, monkeypatch, seen, {"detail": "period_outside_coverage"}, status=404)
    before = await _balance(clients)
    response = await clients.get(
        f"/call/{FUND_HOLDINGS}",
        params={"cik": "1067983", "period": "2020-12-31", "limit": 1})
    assert response.status_code == 404, response.text
    assert await _balance(clients) == before
    (request,) = seen
    assert request.url.params["period"] == "2020-12-31"


def _row_text(endpoint_id: str) -> tuple[str, str]:
    """(all human-readable text on the row, the cost note)."""
    ep = catalog_store.load().by_id[endpoint_id]
    parts = [ep["summary"], (ep.get("input") or {}).get("note") or ""]
    parts += [p.get("note") or "" for p in ep["input"]["queryParams"].values()]
    cost_note = ep["cost"].get("note") or ""
    parts.append(cost_note)
    return " ".join(parts), cost_note


def test_fund_tools_state_coverage_floor_and_new_positions_window():
    for tool in FUND_TOOLS:
        text, _ = _row_text(tool)
        assert "2021-03-31" in text, f"{tool} does not state the 2021-03-31 coverage floor"
    for tool in FUND_TOOLS:
        if tool == FUNDS_SEARCH:
            continue
        text, cost_note = _row_text(tool)
        assert "period_outside_coverage" in text, f"{tool} omits period_outside_coverage"
        assert "period_outside_coverage" in cost_note, (
            f"{tool} cost note omits that period_outside_coverage is unbilled")
    text, _ = _row_text(NEW_POSITIONS)
    assert "newest 4 calendar quarter-ends" in text
    for tool in (CONGRESSIONAL, INSIDER, INTENT, PROBE):
        text, _ = _row_text(tool)
        assert "2021-03-31" not in text, tool
        assert "period_outside_coverage" not in text, tool
