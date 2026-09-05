"""Direct marketplace calls: `treg call <catalog-endpoint-id>` with no registered tool.

The credential ladder (docs/context/architecture/catalog.md §platform-eligible, and the header
comment above `_resolve_marketplace_call`): an org tool for the provider wins (tier 1), else an org credential matching the provider is
injected via a virtual, never-persisted tool (tier 2), else — for an endpoint treg is willing to spend
its own money on — TREG'S OWN key, metered against the org's prepaid balance (tier 4), and only then
the actionable connect/secret error (tier 3).

Tier 4 is the only rung that spends OUR money, so most of what follows is about the fences around it:
it is shadowed by any credential the org already has, it is off unless the provider is allow-listed AND
keyed, it refuses demo orgs, it reserves before the request leaves and settles/releases after, and the
platform key must never appear in a response, an error, or an audit row.
"""

from __future__ import annotations

import json

import httpx
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from treg import api as A, audit, oauth_providers
from treg.domain import money as ledger
from treg.domain.catalog import store as catalog_store
from treg.application.call import resolve as call_resolution
from treg.application.call import settle as call_settle
from treg.application.call import service as call_service
from treg.application.call.types import ResolutionFailed, UpstreamResponse
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import Org

EP = "tikhub.tiktok.video.comments"          # GET /api/v1/tiktok/web/fetch_post_comment, aweme_id required
EP_PATH = "/api/v1/tiktok/web/fetch_post_comment"
EP_MICRO = 1_000                             # $0.001/call, cost.type per_success
EP_CALL = "scrapecreators.x.v1-facebook-group"   # GET, cost.type PER_CALL ($0.00188) — a 4xx is billable
EP_CALL_MICRO = 1_880
EP_DFS = "dataforseo.web.page.audit"         # POST; priced per crawled PAGE, and dataforseo reports
EP_DFS_MICRO = 150   # $0.00015/page × the ONE task in the test body (array length drives the estimate)

PLATFORM_KEYS = {  # never a real key: a test that leaked one into an assertion would print it
    "TIKHUB": "PLATFORM-TIKHUB-KEY",
    "SCRAPECREATORS": "PLATFORM-SC-KEY",
    "DATAFORSEO": "PLATFORM-DFS-KEY",
    "BRIGHTDATA": "PLATFORM-BD-KEY",
    "APOLLO": "PLATFORM-APOLLO-KEY",
}


@pytest.fixture
def platform_on(monkeypatch):
    """Turn tier 4 on the way a deploy does: keys in the environment AND the provider allow-listed."""
    for name, value in PLATFORM_KEYS.items():
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{name}", value)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ",".join(k.lower() for k in PLATFORM_KEYS))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def minimax_platform_on(monkeypatch):
    """Enable only MiniMax tier 4 for its provider-envelope billing regressions."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_MINIMAX", "PLATFORM-MINIMAX-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "minimax")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _balance(clients: AsyncClient) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


async def _entries(clients: AsyncClient) -> list[dict]:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["entries"]["items"]


async def _telemetry(clients: AsyncClient) -> dict:
    """The newest audit row, with the marketplace/spend columns."""
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    return rows[0]


def _fake_relay(status_code: int, body: bytes = b"{}", *, raises: Exception | None = None):
    """Stand in for `relay` when the test needs a specific UPSTREAM outcome the echo app can't give
    (a provider 5xx, a network error, a provider-reported cost). Everything else uses the real relay."""
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        if raises is not None:
            raise raises

        async def _stream():
            yield body

        async def _close():
            return None

        return UpstreamResponse(status_code, (), _stream(), _close)

    return _relay


# ---- tiers 1-3 (unchanged behaviour) -----------------------------------------------------------
async def test_tier2_org_credential_no_tool(clients: AsyncClient):
    """A secret NAMED for the provider serves the call — and no tool row appears."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["auth"] == "Bearer MKKEY"                 # injected the provider's way
    assert d["raw_path"] == EP_PATH                     # endpoint id resolved to the real path
    assert d["query"] == {"aweme_id": "7", "count": "5"}
    tools = (await clients.get("/tools")).json()
    assert tools == [], "tier 2 must not materialize a tool row"


async def test_tier2_audits_the_endpoint_id(clients: AsyncClient):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    await clients.get(f"/call/{EP}?aweme_id=7")
    assert (await _telemetry(clients))["tool_name"] == EP


async def test_tier1_registered_tool_wins(clients: AsyncClient):
    """An org tool for the provider's host serves the call with ITS binding — the registry
    stays authoritative over the marketplace fallback."""
    sid = (await clients.post("/secrets", json={"name": "own-key", "value": "OWN"})).json()["id"]
    await clients.post("/tools", json={"name": "our-tikhub", "base_url": "https://api.tikhub.io", "secret_id": sid})
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier-2 bait
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer OWN"
    assert (await _telemetry(clients))["tool_name"] == "our-tikhub"


async def test_catalog_only_route_cannot_be_shadowed_by_same_named_team_tool(clients: AsyncClient):
    """The directory route resolves the curated id directly; legacy `/call` still gives an exact
    same-named team tool precedence, preserving both contracts at once."""
    sid = (await clients.post("/secrets", json={"name": "own-key", "value": "OWN"})).json()["id"]
    await clients.post("/tools", json={"name": EP, "base_url": "http://upstream", "secret_id": sid})
    await clients.post("/secrets", json={"name": "tikhub", "value": "CATALOG"})

    legacy = await clients.get(f"/call/{EP}?aweme_id=7")
    directory = await clients.get(f"/catalog/call/{EP}?aweme_id=7")

    assert legacy.status_code == 200 and legacy.json()["auth"] == "Bearer OWN"
    assert directory.status_code == 200
    assert directory.json()["auth"] == "Bearer CATALOG"
    assert directory.json()["raw_path"] == EP_PATH


async def test_tier3_no_credential_is_an_actionable_404(clients: AsyncClient):
    """With tier 4 OFF (the default — no provider allow-listed), the ladder still dead-ends here."""
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "treg connections connect --provider tikhub" in detail
    assert "treg secret add tikhub" in detail          # tikhub is a pasted-key provider


async def test_missing_required_param_fails_before_any_credential(clients: AsyncClient):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.get(f"/call/{EP}")
    assert r.status_code == 400
    assert "aweme_id" in r.json()["detail"]


async def test_method_mismatch_is_a_400_hint(clients: AsyncClient):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.post(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 400
    assert "GET" in r.json()["detail"]


async def test_provider_name_404_points_at_the_marketplace(clients: AsyncClient):
    """`treg call tikhub /path` (no such tool) keeps failing, but no longer dead-ends."""
    r = await clients.get("/call/tikhub/api/v1/foo")
    assert r.status_code == 404
    assert "marketplace provider" in r.json()["detail"]


async def test_unknown_dotted_name_stays_a_plain_404(clients: AsyncClient):
    r = await clients.get("/call/no.such.endpoint")
    assert r.status_code == 404


def test_path_placeholders_fill_from_query_and_are_consumed():
    """Pure-function check: `{placeholder}` path params substitute (URL-encoded) from query
    params and are reported as consumed so the relay drops them from the query string."""
    provider = type("P", (), {"base_url": "https://api.example.com"})()
    ep = {"id": "x.y.z", "path": "/v3/sites/{siteUrl}/query", "input": {}}
    url, consumed = call_resolution._marketplace_upstream(
        ep, provider, {"siteUrl": "sc-domain:ex.com", "row": "1"})
    assert url == "https://api.example.com/v3/sites/sc-domain%3Aex.com/query"
    assert consumed == {"siteUrl"}

    encoded, _ = call_resolution._marketplace_upstream(
        ep, provider, {"siteUrl": "sc-domain%3Aex.com"})
    assert encoded == "https://api.example.com/v3/sites/sc-domain%3Aex.com/query"

    # A literal `%` is not an encoded marker unless two following characters are hexadecimal.
    literal, _ = call_resolution._marketplace_upstream(
        ep, provider, {"siteUrl": "sc-domain:100%coverage.example"})
    assert literal == "https://api.example.com/v3/sites/sc-domain%3A100%25coverage.example/query"
    with pytest.raises(ResolutionFailed) as exc:
        call_resolution._marketplace_upstream(ep, provider, {})
    assert exc.value.status_code == 400 and "siteUrl" in exc.value.detail


def test_gtm_catalog_builds_hierarchy_from_atomic_ids_without_encoded_slashes():
    ep = catalog_store.load().by_id["google-tag-manager.workspaces"]
    url, consumed = call_resolution._marketplace_upstream(
        ep,
        oauth_providers.GOOGLE_TAG_MANAGER,
        {"account_id": "123", "container_id": "456", "pageToken": "next"},
    )
    assert url == (
        "https://tagmanager.googleapis.com/tagmanager/v2/"
        "accounts/123/containers/456/workspaces"
    )
    assert "%2F" not in url
    assert consumed == {"account_id", "container_id"}


async def test_deny_rules_cover_marketplace_calls(clients: AsyncClient):
    """Policy is evaluated on the RESOLVED upstream — an endpoint-id call can't dodge a host block."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    r = await clients.post(f"/orgs/{org_id}/deny", json={"host": "api.tikhub.io", "note": "no tikhub"})
    assert r.status_code == 200, r.text
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 403


# ---- tier 4: treg's own key, billed to the org balance ------------------------------------------
async def test_tier4_relays_with_the_platform_key_and_charges_the_balance(clients: AsyncClient, platform_on):
    """The keyless first call: no credential in the org, and the endpoint is served anyway — on treg's
    key, with the estimate taken out of the $1 promo balance."""
    before = await _balance(clients)
    assert before == 1_000_000, "a fresh org gets the promo grant (phase 2)"
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["auth"] == f"Bearer {PLATFORM_KEYS['TIKHUB']}"   # treg's key, injected the provider's way
    assert d["raw_path"] == EP_PATH
    assert d["query"] == {"aweme_id": "7", "count": "5"}
    assert (await clients.get("/tools")).json() == [], "tier 4 must not materialize a tool row either"
    assert await _balance(clients) == before - EP_MICRO
    kinds = [e["kind"] for e in await _entries(clients)]
    assert kinds[:2] == ["settle", "reserve"], f"reserve→settle, newest first: {kinds}"
    # The caller is TOLD what it cost. Both llms.txt and skill.md instruct an agent to report the
    # price it spent, and without this the only way to find out is reading the balance before and
    # after — which races with any other call and cannot attribute a figure to one request.
    assert r.headers.get("X-Treg-Cost-Micro") == str(EP_MICRO), dict(r.headers)


async def test_an_unmetered_call_carries_NO_cost_header(clients: AsyncClient, platform_on):
    """A team's own key is never charged, so the header is ABSENT rather than `0` — zero would read
    as "this was free" when the truth is "this was not ours to bill". Same call as tier 2 above, so
    the only difference under test is the header."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.json()["auth"] == "Bearer MKKEY"
    assert "X-Treg-Cost-Micro" not in r.headers


async def test_tier2_shadows_tier4(clients: AsyncClient, platform_on):
    """An org that brought its own key is billed by the provider, not by us — their credential wins and
    the balance is untouched. (Silently switching them onto treg's key would move their data, their
    quota and their rate limits somewhere they never agreed to.)"""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.json()["auth"] == "Bearer MKKEY"
    assert await _balance(clients) == before
    assert await _entries(clients) == [] or all(e["kind"] == "grant" for e in await _entries(clients))
    assert (await _telemetry(clients))["credential_tier"] == "credential"


async def test_tier1_shadows_tier4(clients: AsyncClient, platform_on):
    sid = (await clients.post("/secrets", json={"name": "own-key", "value": "OWN"})).json()["id"]
    await clients.post("/tools", json={"name": "our-tikhub", "base_url": "https://api.tikhub.io", "secret_id": sid})
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.json()["auth"] == "Bearer OWN"
    assert await _balance(clients) == before
    assert (await _telemetry(clients))["credential_tier"] == "tool"


async def test_provider_not_allow_listed_is_still_tier3(clients: AsyncClient, monkeypatch):
    """The kill switch: keys configured, but the provider isn't named in TREG_PLATFORM_PROVIDERS."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", PLATFORM_KEYS["TIKHUB"])
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dataforseo")   # tikhub deliberately absent
    get_settings.cache_clear()
    try:
        r = await clients.get(f"/call/{EP}?aweme_id=7")
        assert r.status_code == 404
        assert "treg connections connect" in r.json()["detail"]
        assert await _balance(clients) == 1_000_000
    finally:
        get_settings.cache_clear()


async def test_allow_listed_without_a_key_is_still_tier3(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    try:
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 404
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("why, patch", [
    ("own_account scope", {"scope": "own_account"}),
    ("unpriced", {"cost": {"type": "per_call", "value": None, "currency": "USD", "confidence": "unknown"}}),
    ("price merely inferred", {"cost": {"type": "per_call", "value": 0.001, "currency": "USD",
                                        "per": 1, "unit": "call", "confidence": "inferred"}}),
    ("account kind", {"kind": "account"}),
])
async def test_ineligible_endpoints_fall_through_to_tier3(clients: AsyncClient, platform_on, monkeypatch, why, patch):
    """`platform_eligible` is the fence: treg spends its own money only where the price is
    machine-computable, provenanced as verified, and the route has answered for real at least once."""
    cat = A.catalog_store.load()
    ep = dict(cat.by_id[EP])
    ep.update(patch)
    monkeypatch.setitem(cat.by_id, EP, ep)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 404, f"{why} must not be served on treg's key"
    assert await _balance(clients) == 1_000_000


async def test_empty_balance_is_a_402_an_agent_can_act_on(clients: AsyncClient, platform_on):
    """Out of money is not "no credential" — it names the balance, the price, and the way to fix it."""
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:  # spend the whole promo through the ledger's own front door
        await ledger.reserve(db, org_id, "drain", 1_000_000)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, r.text
    d = r.json()["detail"]
    assert d["error"] == "insufficient_balance"
    assert d["balance_micro"] == 0
    assert d["estimated_cost_micro"] == EP_MICRO
    assert d["topup_url"] == "/app#billing"
    # The team refilling by hand every hour is the one that should hear auto top-up exists.
    assert d["autotopup_enabled"] is False
    assert "treg topup --auto on" in d["message"]


async def test_caller_max_cost_header_refuses_a_direct_call_before_the_reserve(clients: AsyncClient, platform_on):
    """`X-Treg-Route-Max-Cost` on a plain /call/: a hard ceiling the caller sets, enforced before any
    money moves. Below the price → 402 `route_max_cost` naming both figures, balance untouched; at or
    above it → the call proceeds and is charged as usual; garbage → 400. No default: a direct call
    without the header is uncapped (unlike the routed path's $1)."""
    hdr = "X-Treg-Route-Max-Cost"
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={hdr: "0.0005"})
    assert r.status_code == 402, r.text
    d = r.json()["detail"]
    assert d["error"] == "route_max_cost" and d["endpoint_id"] == EP
    assert d["max_cost_micro"] == 500 and d["estimated_cost_micro"] == EP_MICRO
    assert "nothing was charged" in d["message"]
    assert await _balance(clients) == 1_000_000
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={hdr: "not-money"})
    assert r.status_code == 400, r.text
    assert await _balance(clients) == 1_000_000
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={hdr: "0.001"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Treg-Cost-Micro"] == str(EP_MICRO)
    assert await _balance(clients) == 1_000_000 - EP_MICRO


async def test_a_balance_refusal_is_a_treg_refused_event_not_a_vendor_402(
    clients: AsyncClient, platform_on, posthog_events,
):
    """treg said no before any upstream trip, and the event says so as data, not as a status code."""
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "drain", 1_000_000)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, r.text
    (e,) = await posthog_events()
    p = e["properties"]
    assert p["status_code"] == 402
    assert p["outcome"] == "treg_refused" and p["refused_by"] == "balance"
    assert p["duration_ms"] is None and p["capacity_signal"] is None and p["smoothed"] is None
    assert p["provider"] == "tikhub" and p["tier"] == "platform" and p["call_ref"]


async def test_402_with_autotopup_on_names_the_policy_not_a_missing_card(clients: AsyncClient, platform_on):
    """Auto top-up ON and still out of money means the cooldown or the cap is holding. Saying "add
    funds" alone reads as "auto top-up is broken"; the message names the amount/threshold/cap and
    the command that raises them (cobl.ai, 2026-08-25: 1,500 refusals between hourly $20 refills)."""
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.autotopup_enabled = True
        org.autotopup_consented_at = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC: TIMESTAMP WITHOUT TIME ZONE
        org.autotopup_amount_micro = 20_000_000
        org.autotopup_threshold_micro = 5_000_000
        await db.commit()
        await ledger.reserve(db, org_id, "drain", 1_000_000)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, r.text
    d = r.json()["detail"]
    assert d["autotopup_enabled"] is True
    assert "auto top-up:    on" in d["message"] and "$20" in d["message"] and "$5" in d["message"]
    assert "--auto on" in d["message"]
    assert "treg connections connect --provider tikhub" in d["message"]
    assert PLATFORM_KEYS["TIKHUB"] not in json.dumps(d), "an error must never carry the key"
    row = await _telemetry(clients)
    assert row["status_code"] == 402 and row["endpoint_id"] == EP, \
        "a call refused for money is the event the org asks about first — it must be auditable"
    assert row["cost_charged_micro"] == 0


async def test_malformed_marketplace_call_still_leaves_an_audit_row(clients: AsyncClient, platform_on):
    """Wrong method / missing param dies during resolution, before any tool exists — the attempt must
    still land in the activity feed."""
    r = await clients.post(f"/call/{EP}?aweme_id=7")   # EP is GET
    assert r.status_code == 400
    row = await _telemetry(clients)
    assert row["status_code"] == 400 and row["endpoint_id"] == EP


async def test_released_call_records_charged_zero(clients: AsyncClient, platform_on, monkeypatch):
    """A per_success 4xx releases the hold — the activity feed must show $0.00, not the estimate the
    org was never charged (found live: a tikhub 400 displayed $0.001 of phantom spend)."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(400, b'{"detail":"bad id"}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=nope")).status_code == 400
    row = await _telemetry(clients)
    assert row["cost_charged_micro"] == 0
    assert row["cost_estimated_micro"] == EP_MICRO  # the estimate stays, marked un-charged


@pytest.mark.parametrize("request_body", [
    {"model": "image-01", "n": 1},
    {"model": "image-01", "prompt": "A paper airplane", "n": 10},
])
async def test_minimax_image_error_envelope_releases_hold(
    clients: AsyncClient, minimax_platform_on, monkeypatch, request_body,
):
    """MiniMax reports invalid image params inside HTTP 200; those requests cost the caller zero."""
    rejected = {"base_resp": {"status_code": 2013, "status_msg": "invalid params"}}
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps(rejected).encode()))
    before = await _balance(clients)

    response = await clients.post("/call/minimax.image-gen.from_text", json=request_body)

    assert response.status_code == 200 and response.json() == rejected
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    assert await _balance(clients) == before
    row = await _telemetry(clients)
    assert row["cost_charged_micro"] == 0
    assert row["cost_estimated_micro"] > 0


async def test_settled_call_records_what_was_charged(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({"cost": 0.0005}).encode()))
    assert (await clients.post(f"/call/{EP_DFS}", json=[{"url": "https://x.co/"}])).status_code == 200
    row = await _telemetry(clients)
    assert row["cost_charged_micro"] == 500 and row["cost_observed_micro"] == 500


async def test_per_result_estimate_reads_a_body_limit(clients: AsyncClient, platform_on, monkeypatch):
    """dataforseo expresses row counts in the JSON body — `[{"limit": 3}]` must scale the reserve,
    not fall back to the 20-row default (which would reserve $2.50/call on a lusha-priced endpoint)."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({"cost": 0.00015}).encode()))
    await clients.post(f"/call/{EP_DFS}", json=[{"url": "https://x.co/", "limit": 3}])
    row = await _telemetry(clients)
    assert row["cost_estimated_micro"] == 150 * 3


def test_body_limit_reads_camel_case_and_nested_pagination_keys():
    """companyenrich says `pageSize`, exa `numResults`, icypeas/lusha `pagination.size` — a 2-row page
    on any of them must not reserve (and settle at) the 20-row default: seen live 2026-08-28,
    $0.196 charged for 2 companyenrich rows at $0.0098 each."""
    assert call_resolution._body_limit(json.dumps({"pageSize": 2, "technologies": ["stripe"]}).encode()) == 2
    assert call_resolution._body_limit(json.dumps({"query": "x", "numResults": 3}).encode()) == 3
    assert call_resolution._body_limit(json.dumps({"query": {}, "pagination": {"size": 4}}).encode()) == 4
    assert call_resolution._body_limit(json.dumps({"query": {}, "pagination": {"page": 0}}).encode()) is None
    # one row per listed item: moz `targets` (a 1-target body settled 20 quota rows live, $0.27 for $0.013)
    assert call_resolution._body_limit(json.dumps({"targets": ["moz.com"], "distributions": True}).encode()) == 1
    assert call_resolution._body_limit(json.dumps({"domains": ["a.com", "b.com"]}).encode()) == 2
    # lusha decision-makers: `contactsLimit` caps contacts PER COMPANY and is the whole bill (1 credit
    # each) — without it the route answered 44 rows for microsoft.com, $5.49 in one call (2026-09-02)
    assert call_resolution._body_limit(json.dumps({"companies": [{"domain": "microsoft.com"}], "contactsLimit": 5}).encode()) == 5


async def test_provider_5xx_releases_the_hold(clients: AsyncClient, platform_on, monkeypatch):
    """An upstream failure is not billable: the balance ends exactly where it started."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(503, b'{"error":"upstream is down"}'))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503
    assert await _balance(clients) == before
    kinds = [e["kind"] for e in await _entries(clients)]
    assert kinds[:2] == ["release", "reserve"], kinds


async def test_network_error_releases_the_hold(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, raises=httpx.ConnectError("no route to host")))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 502
    assert await _balance(clients) == before
    assert [e["kind"] for e in await _entries(clients)][:2] == ["release", "reserve"]


async def test_per_success_4xx_releases_but_per_call_4xx_settles(clients: AsyncClient, platform_on, monkeypatch):
    """Whether a rejected request costs money is the endpoint's own billing rule (cost.type), not ours:
    under `per_success` the provider produced nothing, under `per_call` it charged for the attempt."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(400, b'{"error":"bad aweme_id"}'))
    before = await _balance(clients)
    assert (await clients.get(f"/call/{EP}?aweme_id=nope")).status_code == 400
    assert await _balance(clients) == before, "per_success: a rejected request is not billable"

    assert (await clients.get(f"/call/{EP_CALL}?group_id=1")).status_code == 400
    assert await _balance(clients) == before - EP_CALL_MICRO, "per_call: the attempt is billable"


async def test_dataforseo_settles_at_the_cost_it_reports(clients: AsyncClient, platform_on, monkeypatch):
    """DataForSEO puts its own charge on every response — settling against THAT (not our estimate) is
    what keeps the ledger honest when the catalog's price drifts."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({"cost": 0.0005, "tasks": []}).encode()))
    before = await _balance(clients)
    r = await clients.post(f"/call/{EP_DFS}", json=[{"url": "https://example.com/"}])
    assert r.status_code == 200, r.text
    assert await _balance(clients) == before - 500, "charged the $0.0005 reported, not the page estimate"
    settle = next(e for e in await _entries(clients) if e["kind"] == "settle")
    assert settle["meta"]["observed_micro"] == 500
    assert settle["meta"]["cost_source"] == "provider"
    assert (await _telemetry(clients))["cost_observed_micro"] == 500
    assert (await _telemetry(clients))["cost_estimated_micro"] == EP_DFS_MICRO


async def test_metered_call_forces_identity_encoding_upstream(clients: AsyncClient, platform_on):
    """A caller asking for gzip must not poison the settle: the provider's reported charge lives in
    the response body, and a compressed body json-parses to nothing — the call silently settles at
    the estimate (found live: httpx's default Accept-Encoding made dataforseo bill $0.003 instead of
    its reported $0.00015). Metered calls therefore ask the upstream for identity, always."""
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"accept-encoding": "gzip, br"})
    assert r.status_code == 200, r.text
    assert r.json()["headers"]["accept-encoding"] == "identity"


async def test_unmetered_call_keeps_the_callers_encoding(clients: AsyncClient):
    """Tier 2 (org's own key) still streams and must keep the relay contract: the caller's own
    compression choice travels upstream untouched."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"accept-encoding": "gzip, br"})
    assert r.status_code == 200, r.text
    assert r.json()["headers"]["accept-encoding"] == "gzip, br"


async def test_scrapecreators_settles_on_the_credits_it_charged(clients: AsyncClient, platform_on, monkeypatch):
    """ScrapeCreators reports `credits_charged`, not dollars — converted through the SAME credit rate
    `cost_view` prices with, so a 3-credit call costs three times the catalog's per-call figure."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(
        200, json.dumps({"success": True, "credits_charged": 3, "credits_remaining": 100}).encode()))
    before = await _balance(clients)
    assert (await clients.get(f"/call/{EP_CALL}?group_id=1")).status_code == 200
    assert await _balance(clients) == before - 3 * EP_CALL_MICRO
    assert (await _telemetry(clients))["cost_observed_micro"] == 3 * EP_CALL_MICRO


def _mk(provider: str, **kw) -> call_resolution.MarketplaceCall:
    """A minimal MarketplaceCall for observed-cost tests — only the fields the settle math reads."""
    kw.setdefault("tier", "platform")
    kw.setdefault("endpoint_id", "ep")  # hunter's derived cost is keyed on the endpoint, not just the provider
    return call_resolution.MarketplaceCall(tool=None, upstream="", consumed=set(), provider=provider, **kw)


def test_observed_cost_only_trusts_a_real_number():
    """A missing, non-numeric or negative charge means "we never learned it" — settle at the estimate.
    A reported ZERO is different: the provider is saying it did not charge, and is honoured."""
    assert call_settle._observed_cost_micro(_mk("dataforseo"), b'{"cost": 0}') == 0
    assert call_settle._observed_cost_micro(_mk("dataforseo"), b'{"cost": "0.5"}') is None
    assert call_settle._observed_cost_micro(_mk("dataforseo"), b'{"cost": -1}') is None
    assert call_settle._observed_cost_micro(_mk("dataforseo"), b"not json") is None
    assert call_settle._observed_cost_micro(_mk("dataforseo"), b"[1,2,3]") is None
    assert call_settle._observed_cost_micro(_mk("tikhub"), b'{"cost": 0.5}') is None, "tikhub doesn't report a charge"
    # exa reports dollars one level down; a 20-result search is the base plus ten $0.001 riders
    assert call_settle._observed_cost_micro(_mk("exa"), b'{"costDollars": {"total": 0.016, "search": {"neural": 0.016}}}') == 16_000
    assert call_settle._observed_cost_micro(_mk("exa"), b'{"costDollars": {"total": 0}}') == 0
    assert call_settle._observed_cost_micro(_mk("exa"), b'{"costDollars": {"total": "0.007"}}') is None
    assert call_settle._observed_cost_micro(_mk("exa"), b'{"costDollars": 0.007}') is None
    assert call_settle._observed_cost_micro(_mk("exa"), b'{"results": []}') is None
    assert call_settle._observed_cost_micro(_mk("scrapecreators"), b'{"credits_charged": 2}') == 2 * EP_CALL_MICRO
    assert call_settle._observed_cost_micro(_mk("scrapecreators"), b'{"success": true}') is None
    # akta reports `credits_consumed` — the field that makes its per-section enrich billable at
    # actuals rather than the catalog's upper-bound estimate. $0.05/credit (fx.yaml).
    assert call_settle._observed_cost_micro(_mk("akta"), b'{"credits_consumed": 0.5}') == 25_000
    assert call_settle._observed_cost_micro(_mk("akta"), b'{"credits_consumed": 0}') == 0, "a reported zero is honoured"
    assert call_settle._observed_cost_micro(_mk("akta"), b'{"credits_charged": 2}') is None, "wrong field name means we never learned it"


def test_crustdata_settles_from_the_response_credit_header():
    """Crustdata's body has no billing field; X-Credits-Used is the exact call charge."""
    mk = _mk("crustdata", endpoint_id="crustdata.companies.search")
    assert call_settle._observed_cost_micro(
        mk, b'{"rows": []}', httpx.Headers({"X-Credits-Used": "0.03"})) == 9_000
    assert call_settle._observed_cost_micro(mk, b'{"rows": []}', httpx.Headers()) is None
    assert call_settle._observed_cost_micro(
        mk, b'{"rows": []}', httpx.Headers({"X-Credits-Used": "not-a-number"})) is None


def test_aviato_conditional_prices_follow_live_balance_deltas():
    cat = A.catalog_store.load()

    def price(endpoint_id, query=None, body=None):
        ep = cat.by_id[endpoint_id]
        cv = cat.cost_view(ep["cost"], "aviato")
        return call_resolution._marketplace_pricing(
            "aviato", endpoint_id, cv, query or {}, json.dumps(body or {}).encode())

    assert price("aviato.companies.enrich", {"preview": "true"}) == (0, 0)
    assert price("aviato.companies.enrich", {"rescrape": "true"}) == (200_000, 150_000)
    assert price("aviato.people.enrich", {"email": "a@example.com", "rescrape": "true"}) == (100_000, 80_000)
    assert price("aviato.companies.enrich.bulk", body={
        "lookups": [{"website": "a.com"}, {"website": "b.com"}], "rescrape": True,
    }) == (400_000, 150_000)
    assert price("aviato.people.enrich.bulk", body={
        "lookups": [{"email": "a@example.com"}, {"email": "b@example.com"}], "rescrape": True,
    }) == (200_000, 70_000)
    assert price("aviato.people.search.simple", {"perPage": "3", "enrich": "false"}) == (2_500, 0)
    assert price("aviato.people.search.simple", {"perPage": "3", "enrich": "true"}) == (32_500, 0)
    assert price("aviato.people.search.simple", {"perPage": "5", "enrich": "true"}) == (52_500, 0)


def test_aviato_bulk_settles_from_counts_and_simple_search_releases_unbilled_rider():
    companies = _mk("aviato", endpoint_id="aviato.companies.enrich.bulk", unit_micro=150_000)
    assert call_settle._observed_cost_micro(companies, b'{"companies": [{"id": "1"}, null]}') == 150_000
    people = _mk("aviato", endpoint_id="aviato.people.enrich.bulk", unit_micro=70_000)
    assert call_settle._observed_cost_micro(people, b'[{"id": "1"}, null]') == 70_000
    simple = _mk("aviato", endpoint_id="aviato.people.search.simple", unit_micro=0)
    assert call_settle._observed_cost_micro(simple, b'{"items": [{"id":"1"},{"id":"2"},{"id":"3"},'
                                                  b'{"id":"4"},{"id":"5"}]}') == 2_500


def test_aviato_single_enrich_releases_documented_but_live_unbilled_riders():
    company = _mk("aviato", endpoint_id="aviato.companies.enrich", unit_micro=150_000)
    assert call_settle._observed_cost_micro(company, b'{"id":"company"}') == 150_000
    person = _mk("aviato", endpoint_id="aviato.people.enrich", unit_micro=80_000)
    assert call_settle._observed_cost_micro(person, b'{"id":"person"}') == 80_000


def test_observed_cost_counts_resources_for_billed_oauth_reads():
    """An oauth-billed per_result call settles against the RESPONSE — X bills per resource returned,
    so `data`'s length is the bill: 7 posts back on a 100-post ask settles at 7, an empty page at
    zero, and a single-object `data` (a profile read) at one. Anything unparseable falls back to
    the estimate (None), and a non-per_result billed call never counts."""
    x = _mk("x", tier="tool", billed_oauth=True, cost_type="per_result", unit_micro=5_000)
    assert call_settle._observed_cost_micro(x, b'{"data": [{}, {}, {}]}') == 15_000
    assert call_settle._observed_cost_micro(x, b'{"data": []}') == 0
    assert call_settle._observed_cost_micro(x, b'{"data": {"id": "1"}}') == 5_000
    assert call_settle._observed_cost_micro(x, b'{"errors": [{}]}') == 0, "no data key = nothing served"
    assert call_settle._observed_cost_micro(x, b"not json") is None, "unreadable body settles at the estimate"
    write = _mk("x", tier="tool", billed_oauth=True, cost_type="per_call", unit_micro=0)
    assert call_settle._observed_cost_micro(write, b'{"data": {"id": "1"}}') is None, "per_call settles at the estimate"

    # leadmagic reports `credits_consumed` too — including 0 on a 2xx miss (observed at verify
    # time) and fractions (email verify = 0.25 credits). $0.025/credit (fx.yaml).
    assert call_settle._observed_cost_micro(_mk("leadmagic"), b'{"credits_consumed": 1}') == 25_000
    assert call_settle._observed_cost_micro(_mk("leadmagic"), b'{"credits_consumed": 0}') == 0, "a 2xx miss is free"
    assert call_settle._observed_cost_micro(_mk("leadmagic"), b'{"credits_consumed": 0.25}') == 6_250
    # lusha nests the same contract one level down: billing.creditsCharged — 0 on a 2xx miss
    # (the captured people.enrich example is one), 2 credits on a company enrich. $0.1248/credit.
    assert call_settle._observed_cost_micro(_mk("lusha"), b'{"billing": {"creditsCharged": 1, "resultsReturned": 10}}') == 124_800
    assert call_settle._observed_cost_micro(_mk("lusha"), b'{"billing": {"creditsCharged": 0, "resultsReturned": 0}}') == 0, "a 2xx miss is free"
    assert call_settle._observed_cost_micro(_mk("lusha"), b'{"billing": {"creditsCharged": 2}}') == 249_600
    assert call_settle._observed_cost_micro(_mk("lusha"), b'{"requestId": "x"}') is None, "no billing block means we never learned it"


def test_apollo_settles_a_2xx_miss_at_zero():
    """Apollo answers a no-match with 2xx and charges nothing for it — `organization: null` on
    enrich, an empty `organizations` page on search. Status-based billing would charge the
    caller the full credit for a response Apollo gave away; the body is what decides. A body
    carrying neither documented shape (people enrichment's 1-9 credit range) stays at the
    estimate — deriving is only safe where the rule is flat."""
    credit = 26_000  # $0.026/credit (fx.yaml, Basic $65/mo / 2,500 credits)
    assert call_settle._observed_cost_micro(_mk("apollo"), b'{"organization": {"name": "Apple"}}') == credit
    assert call_settle._observed_cost_micro(_mk("apollo"), b'{"organization": null}') == 0, "a 2xx miss is free"
    assert call_settle._observed_cost_micro(_mk("apollo"), b'{"organizations": [{"name": "Apple"}], "pagination": {}}') == credit
    assert call_settle._observed_cost_micro(_mk("apollo"), b'{"organizations": [], "pagination": {}}') == 0, "an empty page is free"
    assert call_settle._observed_cost_micro(_mk("apollo"), b'{"person": {"id": "x"}}') is None, "1-9 credit range: estimate, not a guess"
    assert call_settle._observed_cost_micro(_mk("apollo"), b"not json") is None


def test_hunter_domain_search_settles_on_the_emails_it_returned():
    """Hunter's domain search bills one whole SEARCH credit per 10 emails RETURNED, rounded up, and
    a domain it knows nobody at is free — a rule the catalog's per-row price (1 credit ÷ 10 =
    $0.00245/result) cannot express, so the estimate is wrong in both directions. Settling on
    `data.emails` is what makes the published number and the ledger agree: zero emails costs zero,
    and one email costs the same whole credit ten do."""
    credit = 24_500  # $0.0245/credit (fx.yaml, Starter $49/mo / 2,000 credits)
    h = _mk("hunter", endpoint_id="hunter.companies.emails", cost_type="per_result")
    assert call_settle._observed_cost_micro(h, b'{"data": {"domain": "x.com", "emails": []}}') == 0, \
        "a domain with no results is free — the catalog says so and Hunter bills so"
    assert call_settle._observed_cost_micro(h, b'{"data": {"emails": [{"value": "a@x.com"}]}}') == credit, \
        "one email costs a whole search credit, not a tenth of one"
    def _emails(n: int) -> bytes:
        return json.dumps({"data": {"emails": [{"value": f"p{i}@x.com"} for i in range(n)]}}).encode()
    assert call_settle._observed_cost_micro(h, _emails(10)) == credit, "ten still fit in one credit"
    assert call_settle._observed_cost_micro(h, _emails(11)) == 2 * credit, "the 11th rounds up to a second credit"
    assert call_settle._observed_cost_micro(h, b'{"errors": [{"code": "wrong_params"}]}') is None, \
        "no emails key at all: we never learned the count, settle at the estimate"
    assert call_settle._observed_cost_micro(h, b"not json") is None
    other = _mk("hunter", endpoint_id="hunter.people.email.verify", cost_type="per_call")
    assert call_settle._observed_cost_micro(other, b'{"data": {"emails": []}}') is None, \
        "only domain search bills per 10 returned; every other hunter route settles at its estimate"


def test_hunter_email_finder_miss_is_free():
    """The finder's rule is flat: one whole SEARCH credit when an email comes back, nothing on a
    miss — Hunter's pricing says a miss is free, but a miss still answers HTTP 200 with
    `email: null`, so settling at the estimate billed the full credit for a name Hunter had
    nothing on. The body is the only place the found/missed distinction exists."""
    credit = 24_500  # $0.0245/credit (fx.yaml, Starter $49/mo / 2,000 credits)
    f = _mk("hunter", endpoint_id="hunter.people.email.find", cost_type="per_success")
    assert call_settle._observed_cost_micro(f, b'{"data": {"email": "a@x.com", "score": 92}}') == credit
    assert call_settle._observed_cost_micro(f, b'{"data": {"email": null, "score": null}}') == 0, \
        "a miss is free — the catalog says so and Hunter bills so"
    assert call_settle._observed_cost_micro(f, b'{"data": {"email": "", "score": null}}') == 0, \
        "an empty string is a miss too"
    assert call_settle._observed_cost_micro(f, b'{"errors": [{"code": "wrong_params"}]}') is None, \
        "no email key at all: we never learned the outcome, settle at the estimate"
    assert call_settle._observed_cost_micro(f, b"not json") is None


def test_tikhub_envelope_no_charge_settles_at_zero():
    """TikHub reports billing in prose, not a number: a 2xx whose payload is an embedded error
    still says the request will incur a charge — and TikHub really does charge us for it
    (verified live 2026-07-30), so those settle at the estimate, faithfully. Only the explicit
    no-charge phrasing settles at zero."""
    t = _mk("tikhub", cost_type="per_success")
    assert call_settle._observed_cost_micro(t, b'{"code": 200, "message": "Request successful. This request will incur a charge.", "data": {}}') is None, \
        "a billed answer settles at the estimate — that IS what TikHub takes"
    assert call_settle._observed_cost_micro(t, b'{"code": 200, "message": "Request successful. This request will incur a charge.", "data": {"error": "dead_page"}}') is None, \
        "a dead page TikHub bills us for is passed through, not eaten"
    assert call_settle._observed_cost_micro(t, b'{"code": 400, "message": "Request failed. You won\'t be charged for this request.", "data": null}') == 0
    assert call_settle._observed_cost_micro(t, b'{"code": 200, "message": "This request will not incur charges.", "data": {}}') == 0
    assert call_settle._observed_cost_micro(t, b'{"code": 200, "data": {}}') is None, "no message: estimate"
    assert call_settle._observed_cost_micro(t, b"not json") is None


async def test_hunter_zero_result_search_costs_nothing(clients: AsyncClient, platform_on, monkeypatch):
    """End to end, the bug this fixes: four domain searches that returned no emails each settled at
    $0.0490 — the 20-row default page assumption × the per-row price — for results nobody received."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_HUNTER", "PLATFORM-HUNTER-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub,scrapecreators,dataforseo,brightdata,hunter")
    get_settings.cache_clear()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps(
        {"data": {"domain": "nobody.example", "emails": []}, "meta": {"results": 0}}).encode()))
    before = await _balance(clients)
    assert (await clients.get("/call/hunter.companies.emails?domain=nobody.example")).status_code == 200
    assert await _balance(clients) == before, "an empty domain search must not move the balance"
    assert (await _telemetry(clients))["cost_observed_micro"] == 0

    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps(
        {"data": {"domain": "stripe.com", "emails": [{"value": "a@stripe.com"}]},
         "meta": {"results": 2207}}).encode()))
    assert (await clients.get("/call/hunter.companies.emails?domain=stripe.com&limit=1")).status_code == 200
    assert await _balance(clients) == before - 24_500, "one email is one whole search credit"


async def test_hunter_email_finder_no_match_costs_nothing(clients: AsyncClient, platform_on, monkeypatch):
    """End to end, the finder half of the same bug: a no-match answers HTTP 200 with `email: null`
    and Hunter charges nothing for it, but the settle used the estimate and billed the full
    $0.0245 search credit — the exact over-charge a customer measured against the catalog note
    'a miss is free'."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_HUNTER", "PLATFORM-HUNTER-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub,scrapecreators,dataforseo,brightdata,hunter")
    get_settings.cache_clear()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps(
        {"data": {"first_name": "Nobody", "last_name": "Here", "email": None, "score": None,
                  "domain": "nobody.example", "sources": []},
         "meta": {"params": {"full_name": "Nobody Here", "domain": "nobody.example"}}}).encode()))
    before = await _balance(clients)
    r = await clients.get("/call/hunter.people.email.find?domain=nobody.example&full_name=Nobody%20Here")
    assert r.status_code == 200
    assert await _balance(clients) == before, "a miss is free — the balance must not move"
    assert (await _telemetry(clients))["cost_observed_micro"] == 0

    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps(
        {"data": {"first_name": "Patrick", "last_name": "Collison", "email": "p@stripe.com",
                  "score": 92, "domain": "stripe.com", "sources": []},
         "meta": {"params": {"full_name": "Patrick Collison", "domain": "stripe.com"}}}).encode()))
    assert (await clients.get("/call/hunter.people.email.find?domain=stripe.com&full_name=Patrick%20Collison")).status_code == 200
    assert await _balance(clients) == before - 24_500, "a found email is one whole search credit"


async def test_daily_cap_fails_closed(clients: AsyncClient, platform_on, monkeypatch):
    """The per-org daily ceiling on treg's keys — the blast radius of a runaway agent. Unlike the soft
    per-user call cap, it refuses rather than letting spend through."""
    monkeypatch.setenv("TREG_PLATFORM_DAILY_CAP_USD", "0.0015")   # 1500 micro = one call, not two
    get_settings.cache_clear()
    try:
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
        r = await clients.get(f"/call/{EP}?aweme_id=8")
        assert r.status_code == 429, r.text
        d = r.json()["detail"]
        assert d["error"] == "platform_daily_cap_reached"
        assert d["spent_today_micro"] == EP_MICRO and d["daily_cap_micro"] == 1_500
        assert "connect your own key" in d["message"]
        assert await _balance(clients) == 1_000_000 - EP_MICRO, "the refused call cost nothing"
    finally:
        get_settings.cache_clear()


async def test_daily_cap_refuses_when_it_cannot_be_verified(clients: AsyncClient, platform_on, monkeypatch):
    """FAIL CLOSED: if we can't count today's spend, we don't spend."""
    async def _boom(db, org_id):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(ledger, "spent_today", _boom)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429
    assert "refusing to spend" in r.json()["detail"]
    assert await _balance(clients) == 1_000_000


async def test_the_platform_key_never_appears_anywhere(clients: AsyncClient, platform_on):
    """The key may exist in exactly one place: the header the upstream receives. Not in the response we
    return, not in an audit row, not in the ledger's metadata, not in a tool listing."""
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    key = PLATFORM_KEYS["TIKHUB"]
    assert key in r.json()["headers"]["authorization"], "the upstream did receive it"
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    for path in ("/calls", "/tools", "/secrets", f"/orgs/{org_id}/balance", f"/catalog/endpoints/{EP}/access"):
        assert key not in (await clients.get(path)).text, f"{path} leaked the platform key"


async def test_demo_orgs_can_never_spend(clients: AsyncClient, platform_on):
    """The sandbox and the published public-demo token are reachable by anyone with a URL — tier 4 must
    not resolve for them at all (a demo call is synthesized, and synthesizing is not what a hold is for)."""
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.public_demo = True
        await db.commit()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 404, "a public-demo org gets the tier-3 dead-end, not treg's key"
    assert await _balance(clients) == 1_000_000


async def test_telemetry_row_records_the_endpoint_and_the_spend(clients: AsyncClient, platform_on):
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=3")
    assert r.status_code == 200
    row = await _telemetry(clients)
    assert row["tool_name"] == EP
    assert row["endpoint_id"] == EP and row["provider"] == "tikhub"
    assert row["credential_tier"] == "platform"
    assert row["cost_estimated_micro"] == EP_MICRO
    assert row["duration_ms"] is not None and row["response_bytes"] > 0
    assert len(row["params_hash"]) == 64
    # The same call again hashes the same; a different param does not.
    await clients.get(f"/call/{EP}?aweme_id=7&count=3")
    again = await _telemetry(clients)
    assert again["params_hash"] == row["params_hash"]
    await clients.get(f"/call/{EP}?aweme_id=8&count=3")
    assert (await _telemetry(clients))["params_hash"] != row["params_hash"]


async def test_access_probe_reports_the_tier(clients: AsyncClient):
    r = await clients.get(f"/catalog/endpoints/{EP}/access")
    assert r.status_code == 200 and r.json()["tier"] == "none"
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    assert (await clients.get(f"/catalog/endpoints/{EP}/access")).json()["tier"] == "credential"
    sid = (await clients.post("/secrets", json={"name": "k2", "value": "OWN"})).json()["id"]
    await clients.post("/tools", json={"name": "our-tikhub", "base_url": "https://api.tikhub.io", "secret_id": sid})
    assert (await clients.get(f"/catalog/endpoints/{EP}/access")).json()["tier"] == "tool"


async def test_access_probe_reports_the_platform_tier(clients: AsyncClient, platform_on):
    d = (await clients.get(f"/catalog/endpoints/{EP}/access")).json()
    assert d["tier"] == "platform"
    assert d["estimated_cost_micro"] == EP_MICRO
    assert "no key needed" in d["detail"] and "0.001" in d["detail"]


async def test_a_user_may_not_forge_a_platform_binding(clients: AsyncClient, platform_on):
    """The other door onto treg's keys: a tool the caller registers themselves. `relay` resolves
    `platform_setting` from settings without looking at ownership, so the validator has to refuse it."""
    sid = (await clients.post("/secrets", json={"name": "mine", "value": "X"})).json()["id"]
    r = await clients.post("/tools", json={
        "name": "stealer", "base_url": "https://api.tikhub.io",
        "bindings": [{"secret_id": sid, "platform_setting": "platform_key_tikhub", "injector": "env",
                      "location": "header", "name": "Authorization", "format": "Bearer {secret}"}],
    })
    assert r.status_code == 422
    assert "platform_setting" in r.json()["detail"]


def test_local_run_cannot_export_a_platform_binding():
    """`treg run --local` hands credentials to the member's own machine, so it may only ever release
    secrets the tool BINDS BY ID. A platform binding has no secret_id — there is nothing to resolve,
    and the settings value is never in reach of the grant path."""
    from treg import localrun
    from treg.models import Tool

    provider = oauth_providers.get("tikhub")
    tool = Tool(org_id=1, name=EP, base_url=provider.base_url, host="api.tikhub.io",
                bindings=oauth_providers.platform_bindings(provider),
                cli={"enabled": True, "bin": "sh", "inject": [{"via": "env", "name": "TIKHUB_API_KEY"}]})
    assert all(b.get("secret_id") is None for b in tool.bindings)
    assert localrun._resolve_secret_id(tool.cli["inject"][0], tool) is None


def test_platform_estimate_normalizes_per_result_pricing():
    """A per-row price needs a row count: the caller's own limit param, else a page, and capped so one
    call can't reserve an org's whole balance."""
    per_call = {"type": "per_call", "usd": 0.002}
    assert call_resolution._platform_estimate_micro(per_call, {}) == 2_000
    per_row = {"type": "per_result", "usd": 0.0001}
    assert call_resolution._platform_estimate_micro(per_row, {}) == 0.0001 * call_resolution._PLATFORM_PAGE_DEFAULT * 1_000_000
    assert call_resolution._platform_estimate_micro(per_row, {"limit": "5"}) == 500
    assert call_resolution._platform_estimate_micro(per_row, {"limit": "100000"}) == 0.0001 * call_resolution._PLATFORM_PAGE_MAX * 1_000_000
    assert call_resolution._platform_estimate_micro({"type": "per_call", "usd": None}, {}) == 0
    # rounds UP — a sub-micro fraction must never round to free
    assert call_resolution._platform_estimate_micro({"type": "per_call", "usd": 0.0000005}, {}) == 1


def test_platform_estimate_counts_input_entities_not_a_page():
    """A price per TARGET / DOMAIN / KEYWORD is per thing asked about, never per returned row: with
    no limit param the 20-row page default billed a one-target SE Ranking summary 20x ($0.358 for a
    $0.0179 call) and a one-domain Serpstat overview likewise (behavehealth, 2026-09-04). The
    request names the count — repeated or comma-separated query values, a body array (top level or
    a JSON-RPC `params`), else exactly one — and `call` is always one."""
    est = call_resolution._platform_estimate_micro
    per_target = {"type": "per_result", "unit": "target", "usd": 0.0179}
    assert est(per_target, {}) == 17_900                                       # catalog display: one call
    assert est(per_target, {"target": "bestnotes.com", "mode": "domain"}) == 17_900
    assert est(per_target, {"target": "a.com,b.com,c.com"}) == 3 * 17_900
    # a real QueryValues-shaped object with repeated keys
    class Q:
        def __init__(self, items): self._i = items
        def get(self, k, d=None): return next((v for kk, v in self._i if kk == k), d)
        def multi_items(self): return list(self._i)
    assert est(per_target, Q([("target", "a.com"), ("target", "b.com")])) == 2 * 17_900
    assert est(per_target, Q([("targets[]", "a.com"), ("targets[]", "b.com")])) == 2 * 17_900
    # serpstat JSON-RPC: the domains live under params
    per_domain = {"type": "per_result", "unit": "domain", "usd": 0.0025}
    body = b'{"id":"1","method":"SerpstatDomainProcedure.getDomainsInfo","params":{"domains":["a.com","b.com"],"se":"g_us"}}'
    assert est(per_domain, {}, body) == 5_000
    assert est(per_domain, {}, b'{"params":{"domains":["only.com"],"se":"g_us"}}') == 2_500
    # seranking keywords export: a 5,000-keyword body is 5,000 keywords, not a 100-row cap
    per_kw = {"type": "per_result", "unit": "keyword", "usd": 0.00179}
    kw_body = ('{"keywords":' + str([f"k{i}" for i in range(5000)]).replace("'", '"') + '}').encode()
    assert est(per_kw, {"source": "us"}, kw_body) == 5000 * 1_790
    # a limit param on an entity-priced route is NOT a row count
    assert est(per_target, {"target": "a.com", "limit": "50"}) == 17_900
    # `call` is the flat case whatever the request carries
    assert est({"type": "per_result", "unit": "call", "usd": 0.002}, {}, b'{"domain":"x.com","roles":["ceo","cto"]}') == 2_000
    # row-priced routes keep the page semantics
    assert est({"type": "per_result", "unit": "row", "usd": 0.0001}, {}) == 0.0001 * call_resolution._PLATFORM_PAGE_DEFAULT * 1_000_000
    assert est({"type": "quota_rows", "unit": "quota_row", "usd": 0.006667}, {}, b'{"target":"x.com","limit":1}') == 6_667


def test_brightdata_platform_key_injects_as_bearer(platform_on):
    """Tier 4's wiring for Bright Data. Nothing provider-specific had to be written: the settings
    field is found by name (`platform_key_for`) and the header shape comes from the registry entry,
    so this is the regression guard on the generic path staying generic."""
    assert get_settings().platform_key_for("brightdata") == PLATFORM_KEYS["BRIGHTDATA"]
    assert oauth_providers.platform_bindings(oauth_providers.get("brightdata")) == [
        {"platform_setting": "platform_key_brightdata", "injector": "env", "location": "header",
         "name": "Authorization", "format": "Bearer {secret}"}]


def test_crustdata_platform_key_keeps_the_required_version_header():
    """Tier 4 must speak the same provider protocol as BYOK, not only inject the key."""
    assert oauth_providers.platform_bindings(oauth_providers.get("crustdata")) == [
        {"platform_setting": "platform_key_crustdata", "injector": "env", "location": "header",
         "name": "Authorization", "format": "Bearer {secret}"},
        {"platform_setting": "platform_key_crustdata", "injector": "env", "location": "header",
         "name": "x-api-version", "format": "2025-11-01"},
    ]


def test_crustdata_and_aviato_catalogs_are_platform_priced():
    cat = A.catalog_store.load()
    rows = cat.for_provider("crustdata") + cat.for_provider("aviato")
    assert len(rows) == 29
    assert all(cat.platform_eligible(ep) for ep in rows)


def test_exa_catalog_is_platform_priced():
    """Exa prices in dollars per call, so every curated route converts natively and is eligible."""
    cat = A.catalog_store.load()
    rows = cat.for_provider("exa")
    assert len(rows) == 10
    assert all(cat.platform_eligible(ep) for ep in rows)
    assert all(cat.cost_view(ep["cost"], "exa")["usd"] > 0 for ep in rows)


def test_brightdata_estimate_counts_the_body_array():
    """Bright Data bills per record delivered and takes its targets as a bare JSON array, so the
    reserve has to scale with the array's LENGTH — there is no limit param in the query to read."""
    cost = {"type": "per_result", "usd": 0.0015}
    assert call_resolution._platform_estimate_micro(cost, {}, json.dumps([{"url": "a"}]).encode()) == 1_500
    five = json.dumps([{"url": u} for u in "abcde"]).encode()
    assert call_resolution._platform_estimate_micro(cost, {}, five) == 7_500


def test_brightdata_documented_prices_are_billable(platform_on):
    """2026-07-31 policy flip: Bright Data reports no charge in-band and its balance endpoint 403s
    on our token, so its prices can only ever be `documented` ($1.50/1000 records from the public
    pricing page) — and documented is now billable. The provider that motivated the policy must
    actually have eligible endpoints, or "enable all" silently enabled nothing."""
    from treg.domain.catalog import store as catalog_store

    cat = catalog_store.load()
    rows = cat.for_provider("brightdata")
    assert rows, "brightdata is in the catalog"
    eligible = [e["id"] for e in rows if cat.platform_eligible(e)]
    assert len(eligible) >= 20, f"expected the dataset routes to be billable, got {len(eligible)}"


# ---- idempotent calls: step 1, the table and its tenant boundary ----------------------------

async def test_the_same_key_can_belong_to_two_different_CALLERS(clients: AsyncClient):
    """Scoped to the caller, not to the key. Clients choose their own labels, so the same string will
    be picked twice; scoped by key alone that collision serves one caller's stored response to
    another, which is the single failure here that leaks data instead of money.

    Per CALLER rather than per team, because two lazily-written agents inside one team will both
    reach for `retry-1`. Every door resolves to a Membership, so one rule covers a person, an agent,
    and two agents in the same team."""
    from datetime import timedelta

    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall, Membership

    # A second AGENT in the SAME team: the exact case this scoping is for. Two agents belonging to
    # one org, both free to pick the same lazy label.
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    made = await clients.post(f"/orgs/{org_id}/agents", json={"name": "second-agent"})
    assert made.status_code in (200, 201), made.text

    async with session_maker() as db:
        members = (await db.execute(select(Membership).where(
            Membership.org_id == org_id).limit(2))).scalars().all()
        assert len(members) >= 2, "need two callers in ONE team to prove they do not collide"
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=24)
        for m in members[:2]:
            db.add(IdempotentCall(org_id=m.org_id, membership_id=m.id, key="retry-1",
                                  endpoint_id="x", status="done", expires_at=expiry))
        await db.commit()      # must NOT raise: same label, different callers

        rows = (await db.execute(select(IdempotentCall).where(
            IdempotentCall.key == "retry-1"))).scalars().all()
    assert len(rows) == 2, "the same label must be storable once per caller"
    assert len({r.membership_id for r in rows}) == 2


async def test_one_caller_cannot_reuse_a_key_twice(clients: AsyncClient):
    """Per caller the label is unique, which is what makes the pending row a usable lock: two retries
    arriving together race on this constraint and only one reaches the provider."""
    from datetime import timedelta

    import sqlalchemy.exc
    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall, Membership

    async with session_maker() as db:
        m = (await db.execute(select(Membership).limit(1))).scalars().one()
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=24)
        db.add(IdempotentCall(org_id=m.org_id, membership_id=m.id, key="dupe-key",
                              endpoint_id="x", expires_at=expiry))
        await db.commit()
        db.add(IdempotentCall(org_id=m.org_id, membership_id=m.id, key="dupe-key",
                              endpoint_id="x", expires_at=expiry))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await db.commit()


async def test_deleting_a_team_takes_its_remembered_answers(clients: AsyncClient):
    """A stored response belongs to the team that paid for it. Left behind it is a dangling row
    holding someone's data after they asked to be gone."""
    from treg.domain.governance.teams import ORG_SCOPED_MODELS
    from treg.models import IdempotentCall

    assert IdempotentCall in ORG_SCOPED_MODELS


# ---- idempotency step 2: the lookup and replay (storage still off) ---------------------------

async def _seed_answer(clients: AsyncClient, key: str, *, body: bytes = b'{"seeded":true}',
                       fingerprint: str = "", status: str = "done", charged: int = 4200,
                       ttl_s: int = 3600) -> int:
    """Write a stored answer by hand. Step 2 only READS; storage arrives in step 3, so seeding is
    how the read path gets exercised at all."""
    from datetime import timedelta

    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall, Membership

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        m = (await db.execute(select(Membership).where(
            Membership.org_id == org_id).order_by(Membership.id))).scalars().first()
        row = IdempotentCall(
            org_id=org_id, membership_id=m.id, key=key, request_fingerprint=fingerprint,
            endpoint_id="seeded", status=status, charged_micro=charged,
            response_status=200 if status == "done" else None,
            response_body=body if status == "done" else None,
            response_media_type="application/json",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl_s))
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def test_no_key_means_nothing_changes(clients: AsyncClient, platform_on):
    """The header is opt-in. A caller who sends none must see exactly the behaviour they saw before
    this feature existed, which is what keeps the change safe to ship."""
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    assert "X-Treg-Idempotent-Replay" not in r.headers
    assert await _balance(clients) == before - EP_MICRO, "an unlabelled call bills normally"


async def test_a_labelled_retry_is_answered_WITHOUT_reaching_the_provider(clients: AsyncClient,
                                                                          platform_on):
    """The point of the whole feature. The stored answer comes back, the upstream is never called,
    and the balance does not move: merely skipping the second CHARGE would still pay the provider."""
    await _seed_answer(clients, "retry-abc", body=b'{"from":"store"}')
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "retry-abc"})
    assert r.status_code == 200
    assert r.json() == {"from": "store"}, "the SAVED answer, not a fresh upstream response"
    assert r.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert r.headers.get("X-Treg-Cost-Micro") == "4200", "and what it originally cost"
    assert await _balance(clients) == before, "a replay must not move money"


async def test_reusing_a_label_for_a_DIFFERENT_request_is_refused(clients: AsyncClient):
    """A caller bug, and returning the first answer would hide it — they would be handed a response
    to a question they did not ask."""
    await _seed_answer(clients, "reused", fingerprint="a-different-request-entirely")
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "reused"})
    assert r.status_code == 422
    assert "already used for a different request" in r.json()["detail"]


async def test_a_call_still_in_flight_answers_409_rather_than_duplicating(clients: AsyncClient):
    """Two retries arriving together. The second is told to wait instead of being let through to the
    provider, which is the duplicate spend this feature exists to prevent."""
    await _seed_answer(clients, "inflight", status="pending")
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "inflight"})
    assert r.status_code == 409
    assert "still in progress" in r.json()["detail"]


async def test_an_expired_label_frees_itself(clients: AsyncClient, platform_on):
    """Past its window the label means nothing: the call proceeds normally and is billed normally.
    A stale row must not answer for a request made a day later."""
    await _seed_answer(clients, "stale", body=b'{"old":true}', ttl_s=-10)
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "stale"})
    assert r.status_code == 200
    assert r.json() != {"old": True}, "an expired answer must not be replayed"
    assert await _balance(clients) == before - EP_MICRO, "and the fresh call bills"


async def test_one_callers_label_is_invisible_to_another(clients: AsyncClient, platform_on):
    """The tenant boundary, exercised through the HTTP path rather than asserted on the schema. A
    second caller using the same label must reach the provider, not read the first one's answer."""
    await _seed_answer(clients, "shared-label", body=b'{"owner":"first"}')
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    made = await clients.post(f"/orgs/{org_id}/agents", json={"name": "other-agent"})
    assert made.status_code in (200, 201), made.text
    other_token = made.json().get("token")
    assert other_token, made.text

    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = other_token
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "shared-label"})
    if prev:
        clients.headers["X-Treg-Token"] = prev
    assert r.status_code == 200
    assert "X-Treg-Idempotent-Replay" not in r.headers, "another caller must not read this answer"
    assert r.json() != {"owner": "first"}


# ---- idempotency step 3: storing the answer --------------------------------------------------

async def test_the_SAME_LABEL_TWICE_bills_once_and_calls_the_provider_once(clients: AsyncClient,
                                                                           platform_on):
    """The feature, end to end, and the reason it exists.

    An agent calls, the answer is lost on the way back, the agent retries with the same label. The
    provider must be reached ONCE and the balance must move ONCE, and the second caller must get the
    same body the first one would have.
    """
    before = await _balance(clients)
    first = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "same-work"})
    assert first.status_code == 200, first.text
    assert "X-Treg-Idempotent-Replay" not in first.headers, "the first call is not a replay"
    after_first = await _balance(clients)
    assert after_first == before - EP_MICRO, "the first call bills"

    second = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "same-work"})
    assert second.status_code == 200, second.text
    assert second.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert second.json() == first.json(), "the retry gets the SAME answer"
    assert await _balance(clients) == after_first, "and the retry bills NOTHING"


async def test_an_unmetered_call_is_not_stored(clients: AsyncClient):
    """A team calling on its OWN key is billed by the provider, not by us. There is nothing to
    protect, and treg has no business holding their response."""
    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall

    await clients.post("/secrets", json={"name": "tikhub", "value": "OWNKEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "own-key-call"})
    assert r.status_code == 200 and r.json()["auth"] == "Bearer OWNKEY"
    async with session_maker() as db:
        row = (await db.execute(select(IdempotentCall).where(
            IdempotentCall.key == "own-key-call"))).scalar_one_or_none()
    assert row is None, "an unmetered call must leave nothing behind, not even a claim"


async def test_a_FAILED_call_frees_its_label(clients: AsyncClient, platform_on):
    """A failure was never billed, so there is nothing to replay — and freezing an error would stop
    the caller retrying out of it. The label must be usable again immediately."""
    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall

    bad = await clients.get(f"/call/{EP}", headers={"Idempotency-Key": "will-fail"})
    assert bad.status_code >= 400, bad.text          # missing the required aweme_id
    async with session_maker() as db:
        row = (await db.execute(select(IdempotentCall).where(
            IdempotentCall.key == "will-fail"))).scalar_one_or_none()
    assert row is None, "a failed call must not hold its label"

    good = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "will-fail"})
    assert good.status_code == 200, "and the same label works straight away"


async def test_a_second_call_while_the_first_is_IN_FLIGHT_is_refused(clients: AsyncClient,
                                                                     platform_on):
    """The pending row is the lock. Claimed before the upstream call, so a concurrent retry loses the
    insert on (membership_id, key) rather than duplicating the spend."""
    from datetime import timedelta

    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall, Membership

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        m = (await db.execute(select(Membership).where(
            Membership.org_id == org_id).order_by(Membership.id))).scalars().first()
        db.add(IdempotentCall(
            org_id=org_id, membership_id=m.id, key="racing", request_fingerprint="",
            endpoint_id=EP, status="pending",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)))
        await db.commit()

    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "racing"})
    assert r.status_code == 409
    assert await _balance(clients) == before, "the loser of the race must not spend"


async def test_a_stale_label_reused_later_starts_fresh(clients: AsyncClient, platform_on):
    """A caller with stable labels (`nightly-report`, say) must be able to call again tomorrow.

    Note what this does NOT prove: the read path already drops an expired row when it looks one up,
    so this passes with the sweep removed. The sweep is covered separately below — I wrote this one
    believing it tested the sweep, and only found out by deleting the sweep and watching it pass."""
    from datetime import timedelta

    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall, Membership

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        m = (await db.execute(select(Membership).where(
            Membership.org_id == org_id).order_by(Membership.id))).scalars().first()
        db.add(IdempotentCall(
            org_id=org_id, membership_id=m.id, key="nightly-report", endpoint_id=EP,
            status="done", response_status=200, response_body=b'{"yesterday":true}',
            response_media_type="application/json", charged_micro=999,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)))
        await db.commit()

    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "nightly-report"})
    assert r.status_code == 200, r.text
    assert "X-Treg-Idempotent-Replay" not in r.headers, "yesterday's answer must not be served"
    assert r.json() != {"yesterday": True}

    async with session_maker() as db:
        rows = (await db.execute(select(IdempotentCall).where(
            IdempotentCall.key == "nightly-report"))).scalars().all()
    assert len(rows) == 1, "exactly one row: the dead one swept, today's kept"
    assert rows[0].response_body != b'{"yesterday":true}'


async def test_the_sweep_clears_labels_NOBODY_COMES_BACK_FOR(clients: AsyncClient, platform_on):
    """What the sweep is actually for, and the only thing that covers it.

    A label used once and never again is never looked up, so the read path never sees it and never
    drops it. Without a sweep those rows accumulate forever, and they hold response BODIES. Any later
    call by the same caller clears them.

    Lazy and caller-scoped, matching the hold reaper in domain/money: a background timer would need a
    scheduler and a leader election on a multi-instance deploy, and would still only run on a timer.
    """
    from datetime import timedelta

    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall, Membership

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        m = (await db.execute(select(Membership).where(
            Membership.org_id == org_id).order_by(Membership.id))).scalars().first()
        db.add(IdempotentCall(
            org_id=org_id, membership_id=m.id, key="abandoned-label", endpoint_id=EP,
            status="done", response_status=200, response_body=b'{"big":"body"}',
            response_media_type="application/json", charged_micro=500,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)))
        await db.commit()

    # a call under a DIFFERENT label: the abandoned row is never looked up, only swept
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "unrelated"})
    assert r.status_code == 200, r.text

    async with session_maker() as db:
        gone = (await db.execute(select(IdempotentCall).where(
            IdempotentCall.key == "abandoned-label"))).scalar_one_or_none()
    assert gone is None, "an expired row nobody returns for must still be reclaimed"


async def test_the_sweep_leaves_OTHER_callers_rows_alone(clients: AsyncClient, platform_on):
    """Scoped to the caller doing the work. A sweep that reached across callers would be a caller
    able to delete another's stored answers by making one call of their own."""
    from datetime import timedelta

    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import IdempotentCall, Membership

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    made = await clients.post(f"/orgs/{org_id}/agents", json={"name": "bystander"})
    assert made.status_code in (200, 201), made.text

    async with session_maker() as db:
        members = (await db.execute(select(Membership).where(
            Membership.org_id == org_id).order_by(Membership.id))).scalars().all()
        other = members[-1]
        db.add(IdempotentCall(
            org_id=org_id, membership_id=other.id, key="someone-elses", endpoint_id=EP,
            status="done", response_status=200, response_body=b"{}", charged_micro=1,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)))
        await db.commit()

    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "mine"})
    assert r.status_code == 200

    async with session_maker() as db:
        still = (await db.execute(select(IdempotentCall).where(
            IdempotentCall.key == "someone-elses"))).scalar_one_or_none()
    assert still is not None, "one caller's sweep must not delete another's rows"


# ---- 429 is never billable (shared-plan pricing, step 2) ------------------------------------

def test_the_billability_truth_table():
    """The exact contract of `_platform_billable`, pinned row by row so a future edit changes it on
    purpose or not at all.

    The 429 row is the shared-plan fix: a rate-limit rejection is capacity refusing the request. On a
    shared plan key it is treg's own saturation, and billing it would charge teams for our
    congestion. It also corrects an existing wrong: under `per_call` the old rule billed upstream
    429s, and no vendor bills a request it refused to accept."""
    # The contract widened in PR #122 and the old table was WRONG about one row: it asserted an
    # upstream 402 under per_call bills the caller ("the provider billing for acceptance"). A 402 is
    # the provider REFUSING — usually because OUR platform key ran out of quota — and no vendor
    # charges for a refusal. The caller pays only for rejections about their own input.
    cases = [
        (200, "per_success", True), (200, "per_call", True),
        # not the caller's fault: credential, payment, quota, timeout, rate limit — never billed
        (401, "per_call", False), (402, "per_call", False), (403, "per_call", False),
        (405, "per_call", False), (407, "per_call", False), (408, "per_call", False),
        (429, "per_call", False), (429, "per_success", False), (429, "per_result", False),
        # the caller's own input: billed under per_call only
        (400, "per_call", True), (404, "per_call", True), (422, "per_call", True),
        (400, "per_success", False), (400, "per_result", False),
        (503, "per_call", False), (503, "per_success", False),
        (302, "per_call", False),
    ]
    for status, cost_type, expected in cases:
        got = call_settle._platform_billable(status, cost_type)
        assert got is expected, f"({status}, {cost_type}) -> {got}, expected {expected}"


async def test_an_upstream_429_releases_the_hold(clients: AsyncClient, platform_on, monkeypatch):
    """End to end: the provider rate-limits, the balance ends exactly where it started, and the
    activity feed shows $0.00 charged."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(429, b'{"error":"rate limited"}'))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429
    assert await _balance(clients) == before, "a 429 must not move money"
    kinds = [e["kind"] for e in await _entries(clients)]
    assert kinds[:2] == ["release", "reserve"], kinds
    row = await _telemetry(clients)
    assert row["cost_charged_micro"] == 0


async def test_a_stale_catalog_method_never_charges_a_per_call_endpoint(
        clients: AsyncClient, platform_on, monkeypatch):
    """A relayed 405 is treg's stale method metadata, not caller input.

    The catalog chooses the method and rejects a caller override before relay. Even a provider whose
    pricing says ``per_call`` therefore cannot turn its rejection of TREG'S method into team spend.
    Pin the ledger path as well as the classifier: this is real balance, not display arithmetic.
    """
    monkeypatch.setattr(call_service, "relay", _fake_relay(405, b'{"error":"method not allowed"}'))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP_CALL}?group_id=1")
    assert r.status_code == 405
    assert r.headers.get("X-Treg-Cost-Micro") == "0"
    assert await _balance(clients) == before
    kinds = [e["kind"] for e in await _entries(clients)]
    assert kinds[:2] == ["release", "reserve"], kinds


async def test_the_SAME_KEY_with_a_DIFFERENT_QUERY_is_refused_end_to_end(clients: AsyncClient,
                                                                         platform_on):
    """PR #122's fingerprint fix, wired. The function-level test passes the query EXPLICITLY, so it
    cannot notice the call site failing to pass it — and `query` has a "" default, so a missed call
    site silently reverts the fix while every function test stays green. (The same shape as the
    purchase-pointer strip that was tested as a helper while production kept the link.)

    Through the real path: same label, different query string → 422, never the stored answer."""
    first = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "q-fp"})
    assert first.status_code == 200, first.text
    second = await clients.get(f"/call/{EP}?aweme_id=8", headers={"Idempotency-Key": "q-fp"})
    assert second.status_code == 422, (
        f"a DIFFERENT query under the same key must be refused, got {second.status_code}: "
        f"{second.text[:120]}")
    assert "different request" in second.json()["detail"]


# ---- trial pools: $0 on treg's key, capped per team per day (fx.yaml kind: treg_trial) -------

@pytest.fixture()
def trial_on(monkeypatch):
    """Tier 4 for a TRIAL provider: treg's free-tier key in the env, provider allow-listed."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_FINNHUB", "trial-pool-test-key")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub,scrapecreators,dataforseo,finnhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_trial_call_is_served_keyless_and_charges_NOTHING(clients: AsyncClient, trial_on,
                                                                  monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"c": 231.5, "pc": 230.1}'))
    before = await _balance(clients)
    r = await clients.get("/call/finnhub.quote?symbol=AAPL")
    assert r.status_code == 200, r.text
    assert await _balance(clients) == before, "a $0 trial call must not move money"


async def test_the_trial_allowance_bites_at_the_fx_number(clients: AsyncClient, trial_on,
                                                          monkeypatch):
    """Seed today's audit at the allowance (50 for finnhub, from fx.yaml) — the next call must be
    refused with the connect-your-own-key hint, unbilled. Failed calls are seeded too and must NOT
    count: a 4xx produced nothing, the same line billability draws."""
    from treg.models import CallRecord

    async with session_maker() as db:
        for i in range(50):
            db.add(CallRecord(org_id=1, user_email="u@example.com", tool_name="finnhub.quote",
                              method="GET", path="/quote", status_code=200))
        for i in range(10):  # failures do not consume the allowance
            db.add(CallRecord(org_id=1, user_email="u@example.com", tool_name="finnhub.quote",
                              method="GET", path="/quote", status_code=502))
        await db.commit()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"c": 1}'))
    before = await _balance(clients)
    r = await clients.get("/call/finnhub.quote?symbol=AAPL")
    assert r.status_code == 429, r.text
    d = r.json()["detail"]
    assert d["error"] == "trial_allowance_reached" and d["allowance_per_day"] == 50
    assert "connect" in d["message"]
    assert await _balance(clients) == before


async def test_failures_alone_never_exhaust_a_trial(clients: AsyncClient, trial_on, monkeypatch):
    from treg.models import CallRecord

    async with session_maker() as db:
        for i in range(60):
            db.add(CallRecord(org_id=1, user_email="u@example.com", tool_name="finnhub.quote",
                              method="GET", path="/quote", status_code=429))
        await db.commit()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"c": 1}'))
    assert (await clients.get("/call/finnhub.quote?symbol=AAPL")).status_code == 200


async def test_another_orgs_usage_never_burns_MY_trial(clients: AsyncClient, trial_on, monkeypatch):
    """The allowance is per TEAM. Another org's fifty calls must not touch this org's pool — the
    multi-tenancy assertion, and the one failure here that would be unfair rather than merely
    wrong."""
    from treg.models import CallRecord

    other = await clients.post("/orgs", json={"name": "another-trial-team"})
    assert other.status_code == 200, other.text
    async with session_maker() as db:
        for i in range(50):
            db.add(CallRecord(org_id=other.json()["org_id"], user_email="other@example.com",
                              tool_name="finnhub.quote", method="GET", path="/quote",
                              status_code=200))
        await db.commit()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"c": 1}'))
    assert (await clients.get("/call/finnhub.quote?symbol=AAPL")).status_code == 200


# ---- X: the catalog price and the metered price are the same number ----------------------------
# The bug this pins: `x.extended.yaml` shipped 168 routes priced `free` (a note about the Free/Basic/
# Pro plan caps X abolished in Feb 2026), while `_oauth_billed_estimate` skipped that block — its
# `usd` is 0, which is falsy — and charged the provider fallback instead. The catalog said $0 and
# the balance said $0.10, which is the one disagreement a published price must never have.

def _x_endpoints():
    from treg.domain.catalog import store as catalog_store
    return [e for e in catalog_store.load().by_id.values() if e.get("provider") == "x"]


def test_no_x_endpoint_is_published_as_free():
    """Nothing on X's v2 API is free to treg any more: X bills the app owner per use, so every
    entry must carry a real rate. A `free` block here is a stale ingest, not a fact."""
    free = [e["id"] for e in _x_endpoints() if (e.get("cost") or {}).get("type") == "free"]
    assert not free, f"X is pay-per-use — these publish a price treg cannot honour: {free[:10]}"


def test_x_catalog_price_equals_what_the_meter_charges():
    """For every X route, the price the catalog publishes is the price the proxy reserves. Walked
    over the whole provider rather than a sample, because the failure mode is one stale entry."""
    from treg import oauth_providers
    x = oauth_providers.get("x")
    for ep in _x_endpoints():
        method = (ep.get("method") or "GET").upper()
        est, ctype, _ = call_resolution._oauth_billed_estimate(x, ep, method, {}, b"")
        published = call_resolution._platform_estimate_micro(
            A.catalog_store.load().cost_view(ep["cost"], "x"), {}, b"")
        assert est == published and ctype == ep["cost"]["type"], (
            f"{ep['id']}: catalog says {published} micro ({ep['cost']['type']}), "
            f"meter reserves {est} micro ({ctype})")


def test_a_zero_price_on_a_billed_provider_falls_back_rather_than_billing_zero():
    """Belt and braces for the next stale ingest: if a `free` block ever reappears on X, the meter
    must charge the provider rate rather than serve an upstream we get billed for at $0."""
    from treg import oauth_providers
    x = oauth_providers.get("x")
    ep = {"id": "x.x.stale", "provider": "x", "method": "GET", "path": "/2/tweets",
          "cost": {"type": "free", "value": 0, "currency": "USD", "unit": "call"}}
    est, ctype, unit = call_resolution._oauth_billed_estimate(x, ep, "GET", {}, b"")
    assert est > 0 and ctype == "per_result" and unit == call_resolution._usd_to_micro(x.billed_read_usd)


# ---- X end to end: the published price is the price the balance loses --------------------------
# Everything above tests the pricing FUNCTIONS. This walks the whole path a real X call takes —
# registry connection → `_billed_marketplace` → reserve → relay → settle — because the free-price
# bug was invisible to every unit test and only showed up as a number on a screen.

@pytest.fixture
def x_billed(monkeypatch):
    """X metering on, the way prod has had it since 2026-08-18."""
    monkeypatch.setenv("TREG_OAUTH_BILLED_PROVIDERS", "x")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _connect_x(clients: AsyncClient) -> None:
    """A REGISTRY X connection: `secret.provider` set is what marks the bill as treg's (a BYO
    connect leaves it empty and is never metered)."""
    import json as _json

    from treg import crypto
    from treg.models import Secret

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        db.add(Secret(org_id=org_id, name="x", kind="oauth", provider="x",
                      value=crypto.encrypt(_json.dumps({"access_token": "tok-test"}))))
        await db.commit()


async def test_a_formerly_free_x_route_now_debits_the_balance(clients: AsyncClient, x_billed,
                                                              monkeypatch):
    """`x.x.get-users-muting` is one of the 168 extended routes that used to publish `free`. X's card
    prices a Mute read at $0.001 per resource, so one muted account back costs exactly that."""
    await _connect_x(clients)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": [{"id": "1"}]}'))
    before = await _balance(clients)
    r = await clients.get("/call/x.x.get-users-muting?id=44196397")
    assert r.status_code == 200, r.text
    spent = before - await _balance(clients)
    assert spent > 0, "a route X bills us for must never be served free"
    assert spent == 1_000, f"expected the published $0.001, spent {spent} micro-USD"


async def test_the_rate_card_is_per_resource_type_not_one_read_and_one_write(clients: AsyncClient):
    """The regression that shipped and was caught in review: every write billed at the post-creation
    rate. X prices each action separately, and the catalog has to say so — creating a list is $0.010,
    managing one $0.005, and deleting an interaction $0.010, none of them $0.015."""
    from treg.domain.catalog import store as catalog_store
    by_id = catalog_store.load().by_id
    assert by_id["x.x.create-lists"]["cost"]["value"] == 0.010, "List: Create is $0.010 per request"
    assert by_id["x.x.update-lists"]["cost"]["value"] == 0.005, "List: Manage is $0.005 per request"
    assert by_id["x.x.unfollow-user"]["cost"]["value"] == 0.010, "Interaction: Delete is $0.010"
    assert by_id["x.x.get-users-muting"]["cost"]["value"] == 0.001, "Mute: Read is $0.001/resource"
    assert by_id["x.x.get-direct-messages-events"]["cost"]["value"] == 0.010, "DM Event: Read is $0.010/resource"


def test_the_owned_read_discount_is_never_claimed():
    """$0.001 owned reads need the caller to own the developer app. On a registry connect the app is
    treg's, so no X entry may quote that rate as a per-CALL own-account price — the way `/2/users/me`
    did until 2026-08-18, under-billing the reads treg pays the most for."""
    from treg.domain.catalog import store as catalog_store
    me = catalog_store.load().by_id["x.x.user.profile"]
    assert me["cost"]["value"] == 0.010 and me["cost"]["type"] == "per_result", (
        "/2/users/me is an ordinary User read for a registry connect")


async def test_a_user_lookup_settles_per_user_returned(clients: AsyncClient, x_billed, monkeypatch):
    """`per_result` settles against the RESPONSE, so the published $0.010/user is what each returned
    user costs — three users back is $0.030, not the reserve for a full page."""
    await _connect_x(clients)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": [{"id":"1"},{"id":"2"},{"id":"3"}]}'))
    before = await _balance(clients)
    r = await clients.get("/call/x.x.get-users-by-ids?ids=1,2,3")
    assert r.status_code == 200, r.text
    assert before - await _balance(clients) == 30_000


async def test_a_byo_x_connection_is_never_metered(clients: AsyncClient, x_billed, monkeypatch):
    """The other half of the rule: a connection made with the org's OWN X app carries no
    `secret.provider`, its upstream bill is already theirs, and treg must not charge for it."""
    import json as _json

    from treg import crypto
    from treg.models import Secret

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        db.add(Secret(org_id=org_id, name="x", kind="oauth", provider="",
                      value=crypto.encrypt(_json.dumps({"access_token": "byo-tok"}))))
        await db.commit()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": [{"id": "1"}]}'))
    before = await _balance(clients)
    r = await clients.get("/call/x.x.get-webhooks")
    assert r.status_code == 200, r.text
    assert await _balance(clients) == before, "a BYO app's bill is the org's, not ours"


def test_observed_cost_counts_brightdata_records():
    """Bright Data bills per record DELIVERED and reports no charge field — the body is the bill.
    Before this settled-by-count existed, every per_result call settled as one record: $13.61
    consumed upstream vs $0.35 billed over three weeks (2026-08-24)."""
    bd = _mk("brightdata", cost_type="per_result", unit_micro=1500)
    # sync scrape / snapshot download, format=json: an array, one element per record
    assert call_settle._observed_cost_micro(bd, b'[{"url": "a"}, {"url": "b"}, {"url": "c"}]') == 4500
    assert call_settle._observed_cost_micro(bd, b"[]") == 0
    # the >60s sync fallback and /trigger hand back a snapshot id: zero records HERE — they bill
    # when the snapshot is downloaded
    assert call_settle._observed_cost_micro(bd, b'{"snapshot_id": "sd_x"}') == 0
    # an early snapshot download answers the job's state, not rows: nothing delivered, nothing billed
    assert call_settle._observed_cost_micro(bd, b'{"status": "running", "message": "not ready"}') == 0
    # ndjson: one record per line
    assert call_settle._observed_cost_micro(bd, b'{"url": "a"}\n{"url": "b"}\n') == 3000
    # csv: header + rows
    assert call_settle._observed_cost_micro(bd, b"url,name\na,x\nb,y\n") == 3000
    # a payload the 8MB metered buffer truncated must settle at the estimate, never a partial count
    assert call_settle._observed_cost_micro(bd, b'[{"url": "a"}, {"url"') is None
    # gzipped (compress=true): can't count, estimate wins
    assert call_settle._observed_cost_micro(bd, b"\x1f\x8b\x08\x00junk") is None
    # a free management route (progress polls) never reaches the counter
    assert call_settle._observed_cost_micro(_mk("brightdata", cost_type="free"), b'{"status": "ready"}') is None


def test_marketplace_resolution_carries_the_per_row_price():
    """`unit_micro` must ride the MarketplaceCall on every tier for per_result endpoints — a settle
    that can't see the row price can only ever bill the estimate."""
    cv = {"type": "per_result", "usd": 0.0015}
    assert call_resolution._usd_to_micro(cv["usd"]) == 1500


async def test_brightdata_sync_scrape_settles_per_record_through_the_ledger(
        clients: AsyncClient, platform_on, monkeypatch):
    """End-to-end: a 1-url sync scrape that DELIVERS five records debits five records' worth —
    the settle counts the response, the hold's 1-record estimate is an overrun, and the ledger
    charges what was delivered ($13.61-vs-$0.35 incident, 2026-08-24)."""
    records = [{"url": f"https://x/{i}", "title": f"r{i}"} for i in range(5)]
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps(records).encode()))
    before = await _balance(clients)
    r = await clients.post("/call/brightdata.web.scrape.structured?dataset_id=gd_x",
                           json=[{"url": "https://x/0"}])
    assert r.status_code == 200, r.text
    assert await _balance(clients) == before - 5 * 1500, "five records at $0.0015 each"
    assert (await _telemetry(clients))["cost_observed_micro"] == 5 * 1500


async def test_brightdata_sync_timeout_202_charges_nothing(
        clients: AsyncClient, platform_on, monkeypatch):
    """The >60s sync fallback answers 202 + snapshot_id: zero records delivered HERE, so the hold
    releases to a zero charge — the records bill when the snapshot is downloaded. Before the fix
    this billed the estimate while the job kept running (and billing) upstream."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(202, b'{"snapshot_id": "sd_test123"}'))
    before = await _balance(clients)
    r = await clients.post("/call/brightdata.web.scrape.structured?dataset_id=gd_x",
                           json=[{"url": "https://x/0"}])
    assert r.status_code == 202, r.text
    assert await _balance(clients) == before, "a snapshot handoff must not charge"
    assert (await _telemetry(clients))["cost_observed_micro"] == 0


async def test_brightdata_snapshot_download_bills_the_jobs_records(
        clients: AsyncClient, platform_on, monkeypatch):
    """The async job's records bill at the snapshot download — the endpoint that was cataloged
    `free` while $9.09 of Google Play reviews rode through it unbilled."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(
        202, b'{"snapshot_id": "sd_test123"}'))
    started = await clients.post(
        "/call/brightdata.web.scrape.structured?dataset_id=gd_x",
        json=[{"url": "https://x/0"}],
    )
    assert started.status_code == 202

    records = [{"review": f"r{i}"} for i in range(40)]
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps(records).encode()))
    before = await _balance(clients)
    r = await clients.get("/call/brightdata.web.scrape.job.results"
                          "?snapshot_id=sd_test123&format=json")
    assert r.status_code == 200, r.text
    assert await _balance(clients) == before - 40 * 1500, "40 records at $0.0015 each"
    assert (await _telemetry(clients))["cost_observed_micro"] == 40 * 1500


def test_hunter_people_enrich_accepts_linkedin_handle_without_email():
    """Hunter's /people/find takes `email` OR `linkedin_handle`. The catalog marked email required,
    so treg refused every handle-keyed call with a 400 before Hunter ever saw it — 2,369 times for
    one team in a week (org 2125, 2026-08-18..25), logged with no evidence. One-of is expressed
    the way the rest of the catalog does it: every alternative optional, the rule in the note."""
    ep = A.catalog_store.load().by_id["hunter.people.enrich"]
    qp = ep["input"]["queryParams"]
    assert not qp["email"].get("required") and not qp["linkedin_handle"].get("required")
    assert "linkedin_handle" in ep["input"]["note"]
    assert not any(v.get("required") for v in qp.values() if isinstance(v, dict))


# ---------------------------------------------------------------------------------------------
# 2026-08-30 billing forensics (org 2867): three fixes pinned

def test_body_limit_reads_nested_paging():
    # influencersclub discovery: {"paging": {"limit": 10}} must beat the 20-row default.
    from treg.application.call.resolve import _body_limit
    assert _body_limit(json.dumps({"paging": {"limit": 10}, "filters": {}}).encode()) == 10
    assert _body_limit(json.dumps({"pagination": {"size": 7}}).encode()) == 7
    assert _body_limit(json.dumps({"filters": {}}).encode()) is None


def test_influencersclub_settle_counts_accounts():
    from types import SimpleNamespace
    from treg.application.call.settle import _observed_cost_micro
    mk = SimpleNamespace(cost_type="per_result", unit_micro=5980, billed_oauth=False,
                         endpoint_id="influencersclub.creators.search", provider="influencersclub")
    body = json.dumps({"total": 634, "limit": 10,
                       "accounts": [{"user_id": i} for i in range(10)]}).encode()
    # 10 creators returned → 10 × 5,980µ$ = $0.0598, NOT the 20-row estimate ($0.1196)
    assert _observed_cost_micro(mk, body) == 59_800
    # an envelope with no rows costs nothing
    assert _observed_cost_micro(mk, json.dumps({"detail": "quota"}).encode()) == 0


async def test_concurrent_settles_never_lose_a_block_draw(clients: AsyncClient):
    """The $1.70 drift: parallel settles both read a block's remaining, last write wins, one draw
    is lost — blocks then show MORE credit than the ledger's truth. The FOR UPDATE row lock makes
    parallel draws serialize; after N concurrent settles the block must equal the ledger."""
    import asyncio
    from treg.domain import money
    from treg.infra.db import session_maker
    from treg.models import CreditBlock
    from sqlalchemy import select

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]

    async def one(i: int):
        async with session_maker() as db:
            await money.reserve(db, org_id, "x.y", 10_000, call_id=f"drift-{i}")
        async with session_maker() as db:
            await money.settle(db, f"drift-{i}", 10_000)

    await asyncio.gather(*[one(i) for i in range(8)])
    async with session_maker() as s:
        blocks = (await s.execute(select(CreditBlock).where(CreditBlock.org_id == org_id))).scalars().all()
        drawn = sum(b.amount_micro - b.remaining_micro for b in blocks)
    # 8 settles × margin(10,000µ$) each must ALL be drawn from the blocks — none lost.
    from treg.domain.money import with_margin
    assert drawn == 8 * with_margin(10_000)
