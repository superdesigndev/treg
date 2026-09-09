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
from treg.application.call import contactout
from treg.application.call import resolve as call_resolution
from treg.application.call import settle as call_settle
from treg.application.call import service as call_service
from treg.application.call.types import ResolutionFailed, UpstreamResponse
from treg.config import get_settings
from sqlalchemy import select
from treg.infra.db import session_maker
from treg.models import LedgerEntry, Org

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


async def test_tier1_two_hand_registered_same_host_tools_stay_ambiguous(clients: AsyncClient):
    """Two hand-registered tools on the provider's host, neither tied to a registry connection:
    nothing says which credential the caller meant, so the catalog call still 409s - and the
    detail names the catalog id, both tools, and the named form that disambiguates."""
    a = (await clients.post("/secrets", json={"name": "key-a", "value": "A"})).json()["id"]
    b = (await clients.post("/secrets", json={"name": "key-b", "value": "B"})).json()["id"]
    await clients.post("/tools", json={"name": "tikhub-a", "base_url": "https://api.tikhub.io", "secret_id": a})
    await clients.post("/tools", json={"name": "tikhub-b", "base_url": "https://api.tikhub.io", "secret_id": b})
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "target_ambiguous"
    assert detail["endpoint_id"] == EP
    assert detail["tools"] == ["tikhub-a", "tikhub-b"]
    assert detail["named_forms"] == [f"/call/tikhub-a{EP_PATH}", f"/call/tikhub-b{EP_PATH}"]
    assert EP in detail["message"] and f"/call/tikhub-a{EP_PATH}" in detail["message"]


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
    # lusha buying-group: `contactsLimit` caps contacts PER COMPANY and is the whole bill (1 credit
    # each) - without it the route answered 44 rows for one company, $5.49 in one call (2026-09-02,
    # on the since-retired decision-makers path, whose legacy handler never honoured the cap)
    assert call_resolution._body_limit(json.dumps({"companies": [{"domain": "microsoft.com"}], "contactsLimit": 5}).encode()) == 5


async def test_retired_lusha_decision_makers_answers_410_with_its_successor_and_reserves_nothing(
    clients: AsyncClient, monkeypatch,
):
    """A cached `lusha.x.decision-makers` call used to reach a legacy handler that ignored the
    `contactsLimit` cap the reservation followed (Lusha removed the route 2026-08-12). The id is now
    a tombstone: the platform key never loads, no hold is placed and the answer names the successor."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_LUSHA", "PLATFORM-LUSHA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "lusha")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(call_service, "relay", _fake_relay(
            200, b'{"results": [], "billing": {"creditsCharged": 44, "resultsReturned": 44}}'))
        before, ledger_before = await _balance(clients), await _entries(clients)
        r = await clients.post("/call/lusha.x.decision-makers",
                               json={"companies": [{"domain": "example.com"}], "contactsLimit": 1})
        assert r.status_code == 410, r.text
        detail = r.json()["detail"]
        assert "lusha.x.decision-makers is retired" in detail
        assert "Use lusha.x.buying-group instead." in detail
        assert "contactsLimit" in detail
        assert await _balance(clients) == before
        assert await _entries(clients) == ledger_before, "no hold was placed, so nothing to settle or release"
    finally:
        get_settings.cache_clear()


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


async def test_a_4xx_bills_only_what_the_provider_reports(clients: AsyncClient, platform_on, monkeypatch):
    """A rejected request is billed on the provider's word, never on the estimate. `per_success`
    releases whatever the body says (nothing was produced). `per_call` MAY bill a caller-input
    rejection — but only when the vendor's own charge field says it took something: a 400 with no
    charge in the body releases the hold (Fiber's "body/identifier Required" and "profile not
    found" billed twenty $0.04 calls to one team on 2026-09-06 under the old settle-at-the-estimate
    rule), while scrapecreators reporting `credits_charged: 1` on the same status settles at that."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(400, b'{"error":"bad aweme_id"}'))
    before = await _balance(clients)
    assert (await clients.get(f"/call/{EP}?aweme_id=nope")).status_code == 400
    assert await _balance(clients) == before, "per_success: a rejected request is not billable"

    r = await clients.get(f"/call/{EP_CALL}?group_id=1")
    assert r.status_code == 400
    assert r.headers.get("X-Treg-Cost-Micro") == "0"
    assert await _balance(clients) == before, "per_call, no charge reported: the hold is released"
    assert [e["kind"] for e in await _entries(clients)][:2] == ["release", "reserve"]
    async with session_maker() as db:
        rel = (await db.execute(select(LedgerEntry).where(LedgerEntry.kind == "release")
                                .order_by(LedgerEntry.created_at.desc()))).scalars().first()
    assert rel.meta.get("reason") == "rejected_unbilled_400"

    monkeypatch.setattr(call_service, "relay", _fake_relay(400, b'{"error":"bad group","credits_charged":1}'))
    assert (await clients.get(f"/call/{EP_CALL}?group_id=1")).status_code == 400
    assert await _balance(clients) == before - EP_CALL_MICRO, "per_call, charge reported: the caller pays that"


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

    # fiber-ai reports `chargeInfo.creditsCharged` on every envelope at $0.02/credit (fx.yaml):
    # a 2-credit profile fetch, a free identity resolve, and — the case that matters — an error
    # body with no `chargeInfo`, which settles as unreported so a per_call 400/404 releases.
    # A poll's "charged-for-async-process" repeats its job's charge and is NOT honoured.
    fiber = _mk("fiber-ai")
    assert call_settle._observed_cost_micro(fiber, b'{"output": {}, "chargeInfo": {"method": "charged-now", "creditsCharged": 2}}') == 40_000
    assert call_settle._observed_cost_micro(fiber, b'{"output": {}, "chargeInfo": {"method": "charged-now", "creditsCharged": 0}}') == 0
    assert call_settle._observed_cost_micro(fiber, b'{"message": "body/identifier Required", "statusCode": 400}') is None
    assert call_settle._observed_cost_micro(fiber, b'{"chargeInfo": {"method": "charged-for-async-process", "creditsCharged": 5}}') is None

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
        # the caller's own input: MAY bill under per_call only — and then only at the charge the
        # provider reports (`test_a_4xx_bills_only_what_the_provider_reports`)
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


@pytest.mark.parametrize(('query', 'count', 'page_size', 'credits'), [
    ('', 0, 10, 0), ('', 10, 10, 1), ('&limit=1', 1, 1, 1),
    ('&limit=10', 6, 10, 1), ('&limit=20', 6, 20, 2),
    ('&limit=50', 50, 50, 5), ('&limit=20&page=1000', 0, 20, 0),
])
async def test_tomba_domain_search_settles_returned_emails(
    clients: AsyncClient, platform_on, monkeypatch, query, count, page_size, credits,
):
    """Bill non-empty pages by page size; empty pages are free regardless of total matches."""
    monkeypatch.setenv('TREG_PLATFORM_KEY_TOMBA', 'SYNTHETIC-TOMBA-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'tomba')
    get_settings.cache_clear()
    body = json.dumps({'data': {
        'domain': 'company.example',
        'emails': [{'email': f'person{i}@company.example'} for i in range(count)],
    }, 'meta': {'total': 100, 'pageSize': page_size}}).encode()
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, body))
    before = await _balance(clients)
    response = await clients.get(f'/call/tomba.companies.emails.list?domain=company.example{query}')
    assert response.status_code == 200
    assert response.content == body
    assert await _balance(clients) == before - credits * 8_900
    telemetry = await _telemetry(clients)
    assert telemetry['cost_estimated_micro'] == max(1, (page_size + 9) // 10) * 8_900
    assert telemetry['cost_observed_micro'] == credits * 8_900
    assert telemetry['cost_charged_micro'] == credits * 8_900


@pytest.mark.parametrize('body', [
    b'{}', b'{"data": {}}', b'{"data": {"emails": null}}',
    b'{"data": {"emails": {}}}', b'{"data": null}', b'not json',
])
def test_tomba_domain_search_unknown_results_keep_estimate(body):
    mk = _mk('tomba', endpoint_id='tomba.companies.emails.list', cost_type='per_result')
    assert call_settle._observed_cost_micro(mk, body) is None


def test_tomba_domain_search_count_does_not_apply_to_other_endpoints():
    mk = _mk('tomba', endpoint_id='tomba.people.email.verify', cost_type='per_call')
    assert call_settle._observed_cost_micro(mk, b'{"data": {"emails": []}}') is None


@pytest.mark.parametrize('page_size', [None, 0, -1, True, "20", 1.5])
def test_tomba_unknown_page_size_does_not_guess_from_email_count(page_size):
    mk = _mk('tomba', endpoint_id='tomba.companies.emails.list', unit_micro=8_900)
    body = json.dumps({'data': {'emails': [{'email': 'person@company.example'}]},
                       'meta': {'pageSize': page_size}}).encode()
    assert call_settle._observed_cost_micro(mk, body) is None


@pytest.mark.parametrize('credits,expected', [(0, 0), (1, 4834), (6, 29004), (-1, None), (True, None), ('1', None), (float('inf'), None)])
def test_quickenrich_settles_reported_credits_at_frozen_rate(credits, expected):
    mk = _mk('quickenrich', endpoint_id='quickenrich.people.email.find', cost_type='per_success', unit_micro=4834)
    assert call_settle._observed_cost_micro(mk, json.dumps({'meta': {'credits_used': credits}}).encode()) == expected


@pytest.mark.parametrize('endpoint,data,title,credits', [
    ('people.email.find', {'email': 'a@example.com'}, '', 1),
    ('people.email.find', {'email': None, 'employee_phone': 'N/A'}, '', 0),
    ('people.phone.find', {'employee_phone': '+15550101000'}, '', 1),
    ('people.phone.find', {'employee_phone': 'N/A'}, '', 0),
    ('people.enrich', {'first_name': 'Example'}, '', 1),
    ('people.enrich', [], '', 0),
    ('people.search.domain', [{'email': 'a@example.com'}, {'employee_phone': '+15550101000'}, {'email': 'N/A'}], '', 1),
    ('people.search.domain', [{'email': 'a@example.com', 'employee_phone': '+15550101000'}, {'email': 'N/A'}], 'CEO', 1),
    ('people.search.domain', [], 'CEO', 0),
    ('companies.search', [{}, {}], '', 2),
    ('companies.search', [], '', 0),
])
def test_quickenrich_fallback_counts_billable_results(endpoint, data, title, credits):
    mk = _mk('quickenrich', endpoint_id='quickenrich.' + endpoint, cost_type='per_success', unit_micro=4834,
             request_data={'queryParams': {'title': title}})
    assert call_settle._observed_cost_micro(mk, json.dumps({'success': True, 'data': data}).encode()) == credits * 4834


@pytest.mark.parametrize('endpoint,query,body,expected', [
    ('people.search.domain', {}, {}, 4834),
    ('people.search.domain', {'title': 'CEO'}, {}, 96680),
    ('companies.search', {}, {}, 48340),
    ('companies.search', {}, {'per_page': 1}, 4834),
    ('companies.search', {}, {'per_page': 100}, 483400),
])
def test_quickenrich_reserves_real_page_size(endpoint, query, body, expected):
    cat = catalog_store.load()
    ep = cat.by_id['quickenrich.' + endpoint]
    cost = cat.cost_view(ep['cost'], 'quickenrich')
    estimate, unit = call_resolution._marketplace_pricing('quickenrich', ep['id'], cost, query, json.dumps(body).encode())
    assert (estimate, unit) == (expected, 4834)


async def test_quickenrich_platform_meter_and_free_discovery(clients, platform_on, monkeypatch):
    monkeypatch.setenv('TREG_PLATFORM_KEY_QUICKENRICH', 'PLATFORM-QUICKENRICH-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'quickenrich')
    get_settings.cache_clear()
    before = await _balance(clients)
    for status, credits, expected in [(200, 1, 4834), (200, 0, 0), (401, 1, 0), (429, 1, 0), (500, 1, 0)]:
        raw = json.dumps({'success': status == 200, 'data': {}, 'meta': {'credits_used': credits}}).encode()
        monkeypatch.setattr(call_service, 'relay', _fake_relay(status, raw))
        response = await clients.get('/call/quickenrich.people.email.find?linkedin_url=https://linkedin.com/in/example')
        assert response.status_code == status and response.content == raw
        before -= expected
        assert await _balance(clients) == before
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, b'{"success":true,"data":[],"meta":{"credits_used":0}}'))
    response = await clients.post('/call/quickenrich.people.search', json={'has_email': True, 'per_page': 1})
    assert response.status_code == 200
    assert await _balance(clients) == before


@pytest.mark.parametrize('endpoint,data,credits,expected', [
    ('people.email.find', {'email': 'person@example.com'}, 1, 4834),
    ('people.phone.find', {'employee_phone': '+15550101000'}, 1, 4834),
    ('people.enrich', {'email': 'person@example.com'}, 1, 4834),
    ('people.email.find', [], 0, 0),
    ('people.phone.find', [], 0, 0),
    ('people.search.domain', [{'email': 'person@example.com'}] * 20, 1, 4834),
    ('people.search.domain', [{'email': 'person@example.com'}] * 6 + [{'email': 'N/A'}] * 2, 6, 29004),
    ('companies.search', [{'company_name': 'Example'}], 1, 4834),
    ('companies.search', [], 0, 0),
])
def test_quickenrich_reported_usage_across_endpoints(endpoint, data, credits, expected):
    """Billing-relevant shapes from live checks; reported usage wins over result count."""
    body = json.dumps({'success': True, 'data': data, 'meta': {'credits_used': credits}}).encode()
    mk = _mk('quickenrich', endpoint_id='quickenrich.' + endpoint, unit_micro=4834)
    assert call_settle._observed_cost_micro(mk, body) == expected


async def test_quickenrich_own_key_wins_without_metering(clients, platform_on, monkeypatch):
    monkeypatch.setenv('TREG_PLATFORM_KEY_QUICKENRICH', 'PLATFORM-QUICKENRICH-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'quickenrich')
    get_settings.cache_clear()
    await clients.post('/secrets', json={'name': 'quickenrich', 'value': 'OWN-QUICKENRICH'})
    before = await _balance(clients)
    response = await clients.get('/call/quickenrich.people.email.find?linkedin_url=https://linkedin.com/in/example')
    assert response.status_code == 200
    assert response.json()['auth'] == 'Bearer OWN-QUICKENRICH'
    assert await _balance(clients) == before
    assert (await _telemetry(clients))['credential_tier'] == 'credential'


@pytest.mark.parametrize('amount,expected', [(0.005,5000),(.0015,1500),(.0025,2500),(0,0),('0.0015',1500), (None,None), (True,None),(-1,None),('NaN',None),('Infinity',None),({},None)])
def test_trykitt_usd_charge(amount, expected):
    mk = _mk('trykitt', endpoint_id='trykitt.people.email.find', cost_type='per_success')
    raw = {'email':'a@example.com','credits':{'jobCredits':amount,'remainingCredits':'900'}}
    assert call_settle._observed_cost_micro(mk,json.dumps(raw).encode()) == expected



def test_trykitt_null_miss_is_free_and_verification_verdicts_are_answers():
    cat = catalog_store.load()
    for email in ['no-results-found',None,'']:
        doc={'email':email,'credits':{'jobCredits':None}}
        assert cat.adapters['trykitt.people.email.find'].is_miss(doc)
        assert call_settle._observed_cost_micro(_mk('trykitt',endpoint_id='trykitt.people.email.find',cost_type='per_success'),json.dumps(doc).encode()) == 0
    for status in ['valid','invalid','unknown','catchall']:
        assert not cat.adapters['trykitt.people.email.verify'].is_miss({'validity':status})
        assert call_settle._observed_cost_micro(_mk('trykitt',endpoint_id='trykitt.people.email.verify',cost_type='per_success'),json.dumps({'validity':status,'credits':{'jobCredits':None}}).encode()) is None



@pytest.mark.parametrize('endpoint,doc,expected', [
    ('trykitt.people.email.find',{'email':'a@example.com','validity':'valid','credits':{'jobCredits':.005}},5000),
    ('trykitt.people.email.find',{'email':'no-results-found','credits':{'jobCredits':None}},0),
    ('trykitt.people.email.find',{'email':'a@example.com','validity':'valid','credits':{'jobCredits':0}},0),
    ('trykitt.people.email.verify',{'validity':'invalid','credits':{'jobCredits':.0015}},1500),
    ('trykitt.people.email.verify',{'validity':'unknown','credits':{'jobCredits':None}},1500),
    ('trykitt.people.email.verify',{'validity':'catchall','credits':{'jobCredits':.0015}},1500),
])
async def test_trykitt_platform_ledger(clients,monkeypatch,kitt_on,endpoint,doc,expected):
    monkeypatch.setattr(call_service,'relay',_fake_relay(200,json.dumps(doc).encode()))
    before=await _balance(clients)
    response=await clients.post('/call/'+endpoint,json={'email':'a@example.com','fullName':'A B','domain':'example.com','realtime':True})
    assert response.status_code == 200,response.text
    assert response.json()==doc
    assert await _balance(clients)==before-expected



@pytest.mark.parametrize('realtime',[None,False,1,'true'])
async def test_trykitt_platform_rejects_non_realtime_before_upstream(clients,monkeypatch,kitt_on,realtime):
    async def fail(*args,**kwargs):
        pytest.fail('must reject before relay')
    monkeypatch.setattr(call_service,'relay',fail)
    body={'email':'a@example.com'}
    if realtime is not None: body['realtime']=realtime
    before=await _balance(clients)
    r=await clients.post('/call/'+'trykitt.people.email.verify',json=body)
    assert r.status_code==400,r.text
    assert await _balance(clients)==before



async def test_trykitt_byok_wins_and_preserves_async_body(clients,monkeypatch,kitt_on):
    await clients.post('/secrets',json={'name':'trykitt','value':'OWN-KITT'})
    # The shared in-process echo upstream exercises real header injection.
    before=await _balance(clients)
    r=await clients.post('/call/'+'trykitt.people.email.verify',json={'email':'a@example.com','realtime':False})
    assert r.status_code==200,r.text
    assert 'OWN-KITT' in r.text
    assert 'TEST-KITT-KEY' not in r.text
    assert await _balance(clients)==before



@pytest.mark.parametrize('amount,expected', [(0, 0), ('0.0000015', 2),
                                           (0.025, 25000), (None, None)])
def test_reported_charge_uses_catalog_path_for_any_provider(monkeypatch, amount, expected):
    endpoint = {'id': 'example.lookup', 'provider': 'example', 'cost': {
        'type': 'per_call', 'value': 0.03, 'currency': 'USD',
        'reported_charge': {'path': 'billing.actual', 'unit': 'usd'},
    }}
    monkeypatch.setitem(catalog_store.load().by_id, endpoint['id'], endpoint)
    body = json.dumps({'billing': {'actual': amount}, 'credits': {'jobCredits': 999}}).encode()
    assert call_settle._observed_cost_micro(
        _mk('example', endpoint_id=endpoint['id']), body) == expected


@pytest.mark.parametrize('body,valid', [
    (b'{"mode":"sync"}', True),
    (b'{"mode":"async"}', False),
    (b'{}', False),
    (b'{"mode":"sync","mode":"sync"}', False),
    (b'[]', False),
])
def test_platform_request_constraints_do_not_require_a_price_table(body, valid):
    ep = {'id': 'example.lookup', 'platform_request': {'body.mode': 'sync'},
          'cost': {'type': 'per_call', 'value': 0.01}}
    if valid:
        call_resolution._enforce_platform_request(ep, body)
    else:
        with pytest.raises(ResolutionFailed):
            call_resolution._enforce_platform_request(ep, body)


# ---- ContactOut ----

@pytest.fixture
def companyenrich_platform_on(monkeypatch):
    """Enable only CompanyEnrich tier 4 for its page-settlement regressions."""
    monkeypatch.setenv('TREG_PLATFORM_KEY_COMPANYENRICH', 'PLATFORM-COMPANYENRICH-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'companyenrich')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _companyenrich_page(count: int) -> bytes:
    return json.dumps({'items': [{'id': i, 'name': f'Person {i}'} for i in range(count)],
                       'page': 1, 'totalPages': 1, 'totalItems': count}).encode()


@pytest.mark.parametrize(('endpoint', 'request_body', 'count', 'unit', 'reserved_rows', 'charged_units'), [
    # people search: 2 credits ($0.0196) per person, 2-credit floor. Live 2026-09-09 an empty
    # pageSize 10 page cost 2 credits upstream while treg settled the 10-row estimate (196,000).
    ('companyenrich.people.search', {'pageSize': 10, 'domains': ['company.example']}, 0, 19_600, 10, 1),
    ('companyenrich.people.search', {'pageSize': 10, 'domains': ['company.example']}, 3, 19_600, 10, 3),
    ('companyenrich.people.search', {'pageSize': 10, 'domains': ['company.example']}, 10, 19_600, 10, 10),
    ('companyenrich.people.search', {'positionQuery': ['Engineer']}, 0, 19_600, 20, 1),
    ('companyenrich.people.search.scroll', {'pageSize': 5, 'domains': ['company.example']}, 0, 19_600, 5, 1),
    ('companyenrich.people.search.scroll', {'pageSize': 5, 'domains': ['company.example']}, 2, 19_600, 5, 2),
    # company search: 1 credit ($0.0098) per company, 1-credit floor; lookalikes 5 per company, 5 floor
    ('companyenrich.companies.search', {'pageSize': 10, 'countries': ['US']}, 0, 9_800, 10, 1),
    ('companyenrich.companies.search', {'pageSize': 10, 'countries': ['US']}, 2, 9_800, 10, 2),
    ('companyenrich.companies.search.scroll', {'pageSize': 3, 'countries': ['US']}, 0, 9_800, 3, 1),
    ('companyenrich.companies.similar', {'pageSize': 4, 'domains': ['company.example']}, 0, 49_000, 4, 1),
    ('companyenrich.companies.similar.scroll', {'pageSize': 4, 'domains': ['company.example']}, 3, 49_000, 4, 3),
])
async def test_companyenrich_search_pages_settle_on_returned_items(
    clients: AsyncClient, companyenrich_platform_on, monkeypatch,
    endpoint, request_body, count, unit, reserved_rows, charged_units,
):
    """The reserve is the requested page; the bill is the returned rows, never below the
    catalog's `minimum_units` floor. Before the rule every 2xx settled at the reserve."""
    body = _companyenrich_page(count)
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, body))
    before = await _balance(clients)
    response = await clients.post(f'/call/{endpoint}', json=request_body)
    assert response.status_code == 200, response.text
    assert response.content == body, 'the relay stays faithful'
    assert await _balance(clients) == before - charged_units * unit
    telemetry = await _telemetry(clients)
    assert telemetry['cost_estimated_micro'] == reserved_rows * unit, 'what the old settle charged'
    assert telemetry['cost_observed_micro'] == charged_units * unit
    assert telemetry['cost_charged_micro'] == charged_units * unit


@pytest.mark.parametrize('body', [
    b'not json', b'\x1f\x8b\x08\x00compressed', b'{"error": "unauthorized"}',
    b'{"items": null}', b'{"items": {"0": {}}}', b'[]', b'',
])
async def test_companyenrich_unreadable_page_settles_at_the_estimate(
    clients: AsyncClient, companyenrich_platform_on, monkeypatch, body,
):
    """No `items` list means the row count is unknown: the hold settles at the reserved page."""
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, body))
    before = await _balance(clients)
    response = await clients.post('/call/companyenrich.people.search',
                                  json={'pageSize': 10, 'domains': ['company.example']})
    assert response.status_code == 200
    assert await _balance(clients) == before - 10 * 19_600
    telemetry = await _telemetry(clients)
    assert telemetry['cost_observed_micro'] is None
    assert telemetry['cost_charged_micro'] == 10 * 19_600


def test_companyenrich_page_floor_is_catalog_data_not_a_number_in_settle():
    """The rule multiplies the catalog floor by the per-row price; endpoints without a declared
    `minimum_units` (enrich, get-by-id, the async routes) keep their existing settlement."""
    cat = catalog_store.load()
    for eid in ('companyenrich.people.search', 'companyenrich.people.search.scroll',
                'companyenrich.companies.search', 'companyenrich.companies.search.scroll',
                'companyenrich.companies.similar', 'companyenrich.companies.similar.scroll'):
        assert cat.by_id[eid]['cost']['minimum_units'] == 1, eid
    mk = _mk('companyenrich', endpoint_id='companyenrich.people.search', cost_type='per_result', unit_micro=19_600)
    assert call_settle._observed_cost_micro(mk, b'{"items": []}') == 19_600
    assert call_settle._observed_cost_micro(mk, b'{"items": [{}, {}, {}]}') == 3 * 19_600
    assert call_settle._observed_cost_micro(mk, b'{"page": 1}') is None
    assert call_settle._observed_cost_micro(_mk('companyenrich', endpoint_id='companyenrich.people.search',
                                                cost_type='per_result', unit_micro=0), b'{"items": []}') is None
    assert 'minimum_units' not in cat.by_id['companyenrich.companies.enrich']['cost']
    flat = _mk('companyenrich', endpoint_id='companyenrich.companies.enrich', cost_type='per_call', unit_micro=9_800)
    assert call_settle._observed_cost_micro(flat, b'{"items": []}') is None


def _contactout_cost(eid):
    return catalog_store.load().cost_view(
        catalog_store.load().by_id["contactout." + eid]["cost"], "contactout"
    )


@pytest.mark.parametrize(
    "work,personal,phone,expected",
    [
        (False, False, False, 0),
        (True, False, False, 150000),
        (False, True, False, 250000),
        (True, False, True, 400000),
        (False, True, True, 500000),
        (True, True, True, 650000),
    ],
)
def test_contactout_contact_hits_are_per_type_per_profile(work, personal, phone, expected):
    c = _contactout_cost("people.linkedin.enrich")
    doc = {
        "status_code": 200,
        "profile": {
            "work_email": ["a@example.test", "b@example.test"] if work else [],
            "personal_email": ["c@example.test"] if personal else [],
            "phone": ["123", "456"] if phone else [],
            "email": ["duplicate-combined@example.test"],
            "contact_availability": {"phone": True},
        },
    }
    assert contactout.observed(c, {"queryParams": {}}, doc) == expected
    assert contactout.estimate(c, {}) == 650000


def test_contactout_search_counts_returned_profiles_and_contacts_not_availability_or_total():
    c = _contactout_cost("people.search.reveal")
    doc = {
        "status_code": 200,
        "metadata": {"total_results": 10000},
        "profiles": {
            "one": {
                "contact_info": {
                    "work_emails": ["a@example.test", "b@example.test"],
                    "phones": ["1"],
                }
            },
            "two": {"contact_availability": {"personal_email": True}},
            "three": {"contact_info": {"personal_emails": ["c@example.test"]}},
        },
    }
    assert contactout.observed(c, {"body": {"reveal_info": True}}, doc) == 710000
    assert contactout.observed(c, {"body": {"reveal_info": False}}, doc) == 60000
    assert contactout.estimate(c, {"page_size": 3, "reveal_info": True}) == 2010000
    assert contactout.estimate(c, {"reveal_info": False}) == 500000
    assert (
        contactout.observed(c, {"body": {}}, {"status_code": 200, "profiles": []}) == 0
    )


def test_contactout_person_and_email_echo_and_search_surcharge():
    doc = {
        "status_code": 200,
        "profile": {
            "email": "input@example.test",
            "workEmail": "work@example.test",
            "phone": "123",
        },
    }
    c = _contactout_cost("people.enrich")
    request = {
        "email": "input@example.test",
        "include": ["work_email", "personal_email", "phone"],
    }
    assert contactout.observed(c, {"body": request}, doc) == 420000
    assert contactout.observed(c, {"body": {}}, doc) == 20000
    assert (
        contactout.observed(
            _contactout_cost("people.email.enrich"),
            {"queryParams": {"email": "input@example.test", "include": "work_email"}},
            doc,
        )
        == 400000
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"name": "A"}, None, {}],
        {"example.test": {"name": "A"}, "missing.test": None},
    ],
)
def test_contactout_company_counts(rows):
    expected = 0 if rows == [] else 20000
    assert (
        contactout.observed(
            _contactout_cost("companies.enrich"), {}, {"status_code": 200, "companies": rows}
        )
        == expected
    )


@pytest.mark.parametrize(
    "eid,params,amount",
    [
        ("people.contact.work", {"email_type": "work"}, 150000),
        (
            "people.contact.work",
            {"email_type": "work", "include_phone": "true"},
            400000,
        ),
        (
            "people.contact.personal",
            {"email_type": "personal", "include_phone": "true"},
            500000,
        ),
        ("people.contact.phone", {"email_type": "none", "include_phone": True}, 250000),
        ("people.enrich", {"include": ["phone"]}, 270000),
        ("companies.enrich", {"domains": ["a.test", "b.test"]}, 40000),
        ("people.linkedin.from-email", {}, 60000),
        ("people.email.verify", {}, 0),
    ],
)
def test_contactout_holds(eid, params, amount):
    assert contactout.estimate(_contactout_cost(eid), params) == amount


async def _contactout_balance(client):
    org = (await client.get("/orgs")).json()[0]["org_id"]
    return (await client.get(f"/orgs/{org}/balance")).json()


@pytest.mark.parametrize(
    "own,status,doc,charge",
    [
        (
            False,
            200,
            {
                "status_code": 200,
                "profile": {"work_email": ["a@example.test"], "phone": ["123"]},
            },
            400000,
        ),
        (False, 200, {"status_code": 200, "profile": {"work_email": []}}, 0),
        (False, 200, {"status_code": 403, "message": "No access"}, 0),
        (False, 403, {"status_code": 403, "message": "Out of credits"}, 0),
        (False, 429, {"status_code": 429}, 0),
        (
            True,
            200,
            {
                "status_code": 200,
                "profile": {"work_email": ["a@example.test"], "phone": ["123"]},
            },
            0,
        ),
    ],
)
async def test_contactout_platform_settles_once_and_own_key_wins(
    clients, contactout_platform, monkeypatch, own, status, doc, charge
):
    if own:
        await clients.post("/secrets", json={"name": "contactout", "value": "OWN-TEST"})

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        assert "/v1/people/linkedin" in upstream_url
        binding = tool.bindings[0]
        assert ("secret_id" in binding) == own
        assert binding["name"] == "token"

        async def stream():
            yield json.dumps(doc).encode()

        async def close():
            pass

        return UpstreamResponse(status, (), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _contactout_balance(clients)
    response = await clients.get(
        "/call/contactout.people.contact.work",
        params={
            "profile": "https://linkedin.com/in/test",
            "email_type": "work",
            "include_phone": "true",
        },
    )
    assert response.status_code == status, response.text
    assert response.json() == doc
    after = await _contactout_balance(clients)
    assert before["balance_micro"] - after["balance_micro"] == charge
    entries = after["entries"]["items"]
    closing = [e for e in entries if e["kind"] in ("settle", "release")]
    assert len(closing) == (0 if own else 1)


async def test_contactout_split_must_be_explicit_and_stats_are_private(clients, contactout_platform):
    response = await clients.get(
        "/call/contactout.people.contact.work",
        params={"profile": "https://linkedin.com/in/test"},
    )
    assert response.status_code == 400
    assert "email_type" in response.text
    response = await clients.get("/call/contactout.account.usage")
    assert response.status_code == 404


def test_contactout_combined_email_array_is_not_billed_twice():
    doc = {
        "status_code": 200,
        "profile": {
            "work_email": ["work@example.test"],
            "personal_email": [],
            "email": ["work@example.test"],
        },
    }
    c = _contactout_cost("people.enrich")
    assert (
        contactout.observed(
            c, {"body": {"include": ["work_email", "personal_email"]}}, doc
        )
        == 170000
    )


async def test_contactout_search_settlement_matches_14_live_results_shape(
    clients, contactout_platform, monkeypatch
):
    doc = {
        "status_code": 200,
        "metadata": {"total_results": 5000},
        "profiles": {"one": {"full_name": "Synthetic"}},
    }

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        async def stream():
            yield json.dumps(doc).encode()

        async def close():
            pass

        return UpstreamResponse(200, (), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _contactout_balance(clients)
    response = await clients.post(
        "/call/contactout.people.search", json={"page_size": 25, "reveal_info": False}
    )
    assert response.status_code == 200, response.text
    after = await _contactout_balance(clients)
    assert before["balance_micro"] - after["balance_micro"] == 20000


async def test_contactout_free_verify_never_reserves_or_settles_money(
    clients, contactout_platform, monkeypatch
):
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        async def stream():
            yield b'{"status_code":200,"data":{"status":"valid"}}'

        async def close():
            pass

        return UpstreamResponse(200, (), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _contactout_balance(clients)
    response = await clients.get(
        "/call/contactout.people.email.verify", params={"email": "test@example.test"}
    )
    assert response.status_code == 200, response.text
    after = await _contactout_balance(clients)
    assert before["balance_micro"] == after["balance_micro"]
    assert all(
        e["amount_micro"] == 0
        for e in after["entries"]["items"]
        if e["kind"] in ("reserve", "settle")
    )


async def test_contactout_transport_failure_releases_hold(clients, contactout_platform, monkeypatch):
    async def relay(*args, **kwargs):
        raise httpx.ReadTimeout("synthetic timeout")

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _contactout_balance(clients)
    response = await clients.get(
        "/call/contactout.people.contact.work",
        params={"profile": "https://linkedin.com/in/test", "email_type": "work"},
    )
    assert response.status_code >= 500
    after = await _contactout_balance(clients)
    assert before["balance_micro"] == after["balance_micro"]
    assert len([e for e in after["entries"]["items"] if e["kind"] == "release"]) == 1


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"{}", b'{"status_code":200}', b'{"status_code":200,"profile":"unexpected"}'])
def test_contactout_contact_reveals_require_recognizable_hit_evidence(body):
    from types import SimpleNamespace
    from treg.application.call.settle import _observed_cost_micro
    mk = SimpleNamespace(provider="contactout", endpoint_id="contactout.people.contact.work",
                         cost_type="per_success", unit_micro=0, billed_oauth=False, request_data={})
    assert _observed_cost_micro(mk, body) == 0


@pytest.mark.parametrize("profile_only", [True, "true", "1"])
@pytest.mark.parametrize("doc,charge", [
    ({"status_code": 200, "profile": {"full_name": "Synthetic Example"}}, 20000),
    ({"status_code": 200, "profile": {}}, 0),
    ({"status_code": 200, "profile": []}, 0),
    ({"status_code": 403, "profile": {"full_name": "Synthetic Example"}}, 0),
])
def test_contactout_profile_only_found_and_miss_billing(profile_only, doc, charge):
    cost = _contactout_cost("people.linkedin.enrich")
    request = {"profile_only": profile_only}
    assert contactout.estimate(cost, request) == 20000
    assert contactout.observed(cost, {"queryParams": request}, doc) == charge


@pytest.mark.parametrize("own", [False, True])
@pytest.mark.parametrize("found", [False, True])
async def test_contactout_profile_only_direct_ledger(clients, contactout_platform, monkeypatch, own, found):
    if own:
        await clients.post("/secrets", json={"name": "contactout", "value": "OWN-TEST"})
    doc = {"status_code": 200, "profile": {"full_name": "Synthetic Example"} if found else {}}
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        async def stream():
            yield json.dumps(doc).encode()
        async def close():
            pass
        return UpstreamResponse(200, (), stream(), close)
    monkeypatch.setattr(call_service, "relay", relay)
    before = await _contactout_balance(clients)
    response = await clients.get("/call/contactout.people.linkedin.enrich",
        params={"profile": "https://www.linkedin.com/in/synthetic", "profile_only": "true"})
    assert response.status_code == 200
    assert response.json() == doc
    after = await _contactout_balance(clients)
    assert before["balance_micro"] - after["balance_micro"] == (20000 if found and not own else 0)
    closing = [e for e in after["entries"]["items"] if e["kind"] in ("settle", "release")]
    assert len(closing) == (0 if own else 1)


@pytest.mark.parametrize("size", [None, 0, 26, True, "1"])
async def test_contactout_reveal_requires_explicit_valid_page_size(clients, contactout_platform, monkeypatch, size):
    async def relay(*args, **kwargs):
        pytest.fail("Invalid page_size must be refused before upstream")
    monkeypatch.setattr(call_service, "relay", relay)
    body = {"reveal_info": True}
    if size is not None:
        body["page_size"] = size
    before = await _contactout_balance(clients)
    response = await clients.post("/call/contactout.people.search.reveal", json=body)
    assert response.status_code == 400
    assert response.json()["detail"]["parameter"] == "page_size"
    after = await _contactout_balance(clients)
    assert before == after


@pytest.mark.parametrize("own,size", [(False, 1), (True, None)])
async def test_contactout_reveal_small_page_and_own_key_relay(clients, contactout_platform, monkeypatch, own, size):
    if own:
        await clients.post("/secrets", json={"name": "contactout", "value": "OWN-TEST"})
    body = {"reveal_info": True}
    if size is not None:
        body["page_size"] = size
    doc = {"status_code": 200, "profiles": {"synthetic": {"full_name": "Synthetic Example"}}}
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        assert json.loads(b"".join([chunk async for chunk in request.body_stream()])) == body
        async def stream():
            yield json.dumps(doc).encode()
        async def close():
            pass
        return UpstreamResponse(200, (), stream(), close)
    monkeypatch.setattr(call_service, "relay", relay)
    before = await _contactout_balance(clients)
    response = await clients.post("/call/contactout.people.search.reveal", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == doc
    after = await _contactout_balance(clients)
    assert before["balance_micro"] - after["balance_micro"] == (0 if own else 20000)
    reserves = [e for e in after["entries"]["items"] if e["kind"] == "reserve"]
    assert len(reserves) == (0 if own else 1)
    assert contactout.estimate(_contactout_cost("people.search.reveal"), {"reveal_info": True, "page_size": 1}) == 670000


# ---- icypeas: async submissions settle at 0, bulk jobs reserve per row ----------------------------
ICYPEAS_CREDIT = 19_000  # $0.019 per credit (fx.yaml); 1 credit per found email
ICYPEAS_ACK = b'{"success": true, "item": {"_id": "mP6hHKABeMoKaEB1K1HF", "status": "NONE"}}'
ICYPEAS_BULK_ACK = b'{"success": true, "status": "in_progress", "file": "L3mhHKAB9iupLhv96W-F"}'
ICYPEAS_SYNC_ROWS = json.dumps({"success": True, "data": [
    {"result": "https://www.linkedin.com/in/example-one", "status": "FOUND", "searchId": "a1"},
    {"result": None, "status": "NOT_FOUND", "searchId": "a2"},
]}).encode()


@pytest.fixture
def icypeas_platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "SYNTHETIC-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---- serpstat: the JSON-RPC envelope is the bill (2026-09-09) ---------------------------------
# Serpstat meters one API credit ("line") per RETURNED row and answers a rejected request as
# HTTP 200 with an `error` object. Verified live against the account's own limits meter: an error
# envelope cost 0 lines while treg settled the 20-row estimate (10,000 micro), and a getKeywordTop
# that returned 12 rows cost exactly 12 lines while treg settled 20 credits. Two estimator misses
# fed that: `_body_limit` never looked inside the JSON-RPC `params` (so `size` was ignored), and the
# row-priced routes carried `unit: keyword`/`domain`, which the entity counter read as "one input".
SERPSTAT_CREDIT = 500  # $0.00050 per API credit (fx.yaml serpstat) in micro-USD
SERP_EP = "serpstat.google.serp.organic"


def _rpc(method: str, **params) -> dict:
    return {"id": "1", "method": method, "params": params}


def _serp_rows(n: int) -> list[dict]:
    return [{"position": i + 1, "url": f"https://site{i}.example/", "domain": f"site{i}.example"}
            for i in range(n)]


@pytest.fixture
def serpstat_platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_SERPSTAT", "SYNTHETIC-SERPSTAT-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "serpstat")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_icypeas_email_find_ack_settles_at_zero_and_closes_the_hold(
        clients: AsyncClient, icypeas_platform_on, monkeypatch):
    """/email-search answers 2xx with {item: {_id, status}} and no result: the hit that costs a
    credit is only visible later on the free poll route. The hold reserved the 20-row page default
    ($0.38) and used to settle at it, hit or miss (every platform success since 2026-08-20 charged
    exactly 380000 micro; NOT_FOUND verified free upstream 2026-09-09). The ack now settles at 0:
    the balance ends where it started and the reserve is closed by a settle, not left dangling."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, ICYPEAS_ACK))
    before = await _balance(clients)
    r = await clients.post("/call/icypeas.people.email.find",
                           json={"firstname": "Jane", "lastname": "Doe", "domainOrCompany": "example.com"})
    assert r.status_code == 200, r.text
    assert r.content == ICYPEAS_ACK, "the relay stays faithful"
    assert await _balance(clients) == before, "an acknowledgement must not charge"
    row = await _telemetry(clients)
    assert row["cost_estimated_micro"] == 20 * ICYPEAS_CREDIT == 380_000, "the old charge, now only the hold"
    assert row["cost_observed_micro"] == 0
    assert row["cost_charged_micro"] == 0
    assert [e["kind"] for e in await _entries(clients)][:2] == ["settle", "reserve"]


async def test_icypeas_bulk_search_reserves_one_row_per_submitted_row(
        clients: AsyncClient, icypeas_platform_on, monkeypatch):
    """A 25-row /bulk-search body holds 25 credits, not the 20-row page default - the rows live in
    the top-level `data` array, which the generic body reader does not count. The job's
    {file, status: in_progress} acknowledgement then settles at 0 like the single route."""
    rows = [["Jane", "Doe", f"company{i}.example"] for i in range(25)]
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, ICYPEAS_BULK_ACK))
    before = await _balance(clients)
    r = await clients.post("/call/icypeas.bulk.search",
                           json={"name": "q3 prospects", "task": "email-search", "data": rows})
    assert r.status_code == 200, r.text
    assert r.content == ICYPEAS_BULK_ACK
    assert await _balance(clients) == before
    row = await _telemetry(clients)
    assert row["cost_estimated_micro"] == 25 * ICYPEAS_CREDIT == 475_000
    assert row["cost_observed_micro"] == 0
    assert row["cost_charged_micro"] == 0


def test_icypeas_bulk_reserve_is_scoped_capped_and_defaults_without_rows():
    cat = A.catalog_store.load()

    def price(endpoint_id, body):
        ep = cat.by_id[endpoint_id]
        cv = cat.cost_view(ep["cost"], "icypeas")
        return call_resolution._marketplace_pricing("icypeas", endpoint_id, cv, {}, json.dumps(body).encode())

    unit = ICYPEAS_CREDIT
    assert price("icypeas.bulk.search", {"task": "email-search", "data": [["A", "B", "a.example"]] * 3}) == (3 * unit, unit)
    # capped like every other row count: a 5,000-row job cannot hold an org's whole balance
    assert price("icypeas.bulk.search", {"task": "email-search", "data": [["A", "B", "a.example"]] * 5000}) == (100 * unit, unit)
    # no usable `data` -> the generic page default, exactly as before
    for body in ({"task": "email-search"}, {"data": []}, {"data": "rows"}, {"data": None}):
        assert price("icypeas.bulk.search", body) == (20 * unit, unit)
    # other icypeas routes with a `data` array are untouched (they keep the page default) ...
    assert price("icypeas.people.identity.resolve.bulk",
                 {"data": ["a@example.com", "b@example.com"]}) == (20 * 10 * unit, 10 * unit)
    # ... and the generic body reader still does not treat `data` as a row list for anyone
    assert call_resolution._body_limit(b'{"data": [1, 2, 3]}') is None


@pytest.mark.parametrize("body", [ICYPEAS_ACK, ICYPEAS_BULK_ACK])
@pytest.mark.parametrize("endpoint_id,cost_type", [
    ("icypeas.people.email.find", "per_result"),
    ("icypeas.bulk.search", "per_result"),
    ("icypeas.companies.emails.role", "per_success"),
])
def test_icypeas_ack_shapes_settle_at_zero(body, endpoint_id, cost_type):
    mk = _mk("icypeas", endpoint_id=endpoint_id, cost_type=cost_type, unit_micro=ICYPEAS_CREDIT)
    assert call_settle._observed_cost_micro(mk, body) == 0


@pytest.mark.parametrize("body", [
    ICYPEAS_SYNC_ROWS,                                   # a synchronous answer carrying rows
    b'{"success": true, "data": []}',                    # rows present, just none found
    b'{"success": true, "items": [], "total": 0}',       # a poll page
    b'{"success": false, "item": {"_id": "x"}}',         # not a success
    b'{"item": {"_id": "x", "status": "NONE"}}',         # no success flag at all
    b'{"success": true, "item": {"status": "NONE"}}',    # no id
    b'{"success": true, "file": "x"}',                   # bulk shape without a status
    b'{"success": true}', b'[]', b'not json', b'',
])
def test_icypeas_non_ack_bodies_keep_the_estimate(body):
    """Only the two acknowledgement shapes settle at zero; a synchronous body with `data` rows -
    identity.resolve.bulk, profile.url.bulk, scrape.bulk - and anything unrecognised keep the
    existing behaviour (the estimate)."""
    mk = _mk("icypeas", endpoint_id="icypeas.people.identity.resolve.bulk", cost_type="per_result",
             unit_micro=10 * ICYPEAS_CREDIT)
    assert call_settle._observed_cost_micro(mk, body) is None


def test_icypeas_ack_rule_leaves_per_call_verify_and_the_free_poll_route_alone():
    """/email-verification is charged per address TESTED, so its ack settles at the estimate; the
    poll route is free in the catalog and must stay so (it is where the provider's charge shows)."""
    mk = _mk("icypeas", endpoint_id="icypeas.people.email.verify", cost_type="per_call")
    assert call_settle._observed_cost_micro(mk, ICYPEAS_ACK) is None
    poll = A.catalog_store.load().by_id["icypeas.search.results.read"]
    assert poll["cost"]["type"] == "free" and poll["cost"]["value"] == 0
def test_body_limit_reads_jsonrpc_params():
    """A JSON-RPC envelope carries the request under `params`; the row signal lives there."""
    lim = call_resolution._body_limit
    assert lim(json.dumps(_rpc("SerpstatKeywordProcedure.getKeywordTop", keyword="seo", se="g_us", size=10)).encode()) == 10
    # no size: no signal, so the page default applies rather than a guess
    assert lim(json.dumps(_rpc("SerpstatKeywordProcedure.getKeywordTop", keyword="seo", se="g_us")).encode()) is None
    # `size` beats the optional `keywords` FILTER list that ranked_keywords also accepts
    assert lim(json.dumps(_rpc("SerpstatDomainProcedure.getDomainKeywords", domain="a.example", se="g_us",
                               keywords=["a", "b"], size=100)).encode()) == 100
    # not an envelope: no `method`, or `params` is not an object
    assert lim(b'{"params": {"size": 10}}') is None
    assert lim(b'{"method": "x", "params": [{"size": 10}]}') is None
    # a top-level limit still wins over the envelope, and plain bodies are unchanged
    assert lim(b'{"method": "x", "limit": 3, "params": {"size": 10}}') == 3
    assert lim(b'{"pagination": {"size": 4}}') == 4


def test_serpstat_row_priced_routes_reserve_the_requested_size():
    """The catalog's real cost blocks, priced through fx.yaml: `size` is the reserve on every route
    priced per RETURNED row, and the request's inputs stay the reserve where the price is per input."""
    cat = catalog_store.load()

    def est(ep_id: str, **params) -> int:
        ep = cat.by_id[ep_id]
        method = ep["input"]["body"]["method"]["example"]
        return call_resolution._platform_estimate_micro(
            cat.cost_view(ep["cost"], "serpstat"), {}, json.dumps(_rpc(method, **params)).encode())

    assert est(SERP_EP, keyword="seo", se="g_us", size=10) == 10 * SERPSTAT_CREDIT
    assert est("serpstat.google.domain.ranked_keywords", domain="a.example", se="g_us", size=100) == 100 * SERPSTAT_CREDIT
    assert est("serpstat.google.keywords.ideas", keyword="seo", se="g_us", size=25) == 25 * SERPSTAT_CREDIT
    assert est("serpstat.web.linking_domains.list", query="a.example", size=40) == 40 * SERPSTAT_CREDIT
    # the reserve is capped: the settle trues up a 1,000-row page from the rows that come back
    assert est(SERP_EP, keyword="seo", se="g_us", size=1000) == call_resolution._PLATFORM_PAGE_MAX * SERPSTAT_CREDIT
    # no size: the page default, never the one-input reading the old `unit: keyword` produced
    assert est("serpstat.google.domain.ranked_keywords", domain="a.example", se="g_us") == call_resolution._PLATFORM_PAGE_DEFAULT * SERPSTAT_CREDIT
    # priced per INPUT: the batch volume and overview methods count what the request names
    assert est("serpstat.google.keywords.volume", keywords=["a", "b", "c"], se="g_us") == 3 * SERPSTAT_CREDIT
    assert est("serpstat.google.domain.overview", domains=["a.example", "b.example"], se="g_us") == 2 * 5 * SERPSTAT_CREDIT


@pytest.mark.parametrize(("size", "envelope", "estimate_credits", "credits"), [
    # the live error case: HTTP 200 + `error`, 0 lines upstream, 20 credits charged before the fix
    (None, {"id": "1", "error": {"code": -32000, "message": "Invalid params"}}, 20, 0),
    # the live success case: 12 rows cost 12 lines, 20 credits charged before the fix
    (None, {"id": "1", "result": {"data": {"top": _serp_rows(12)}, "summary_info": {"left_lines": 988}}}, 20, 12),
    # `size` is the reserve; the rows are the charge
    (10, {"id": "1", "result": {"data": {"top": _serp_rows(10)}, "summary_info": {"left_lines": 978}}}, 10, 10),
    (10, {"id": "1", "result": {"data": {"top": _serp_rows(3)}, "summary_info": {"left_lines": 975}}}, 10, 3),
    # a served result with no rows bills the documented 1-credit minimum, an error never does
    (10, {"id": "1", "result": {"data": {"top": []}, "summary_info": {"left_lines": 974}}}, 10, 1),
    (10, {"id": "1", "error": {"code": -32000, "message": "Limit exceeded"}}, 10, 0),
])
async def test_serpstat_settles_on_the_envelope_rows(
    clients: AsyncClient, serpstat_platform_on, monkeypatch, size, envelope, estimate_credits, credits,
):
    params = {"keyword": "example keyword", "se": "g_us"}
    if size is not None:
        params["size"] = size
    body = json.dumps(envelope).encode()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, body))
    before = await _balance(clients)
    response = await clients.post(f"/call/{SERP_EP}", json=_rpc("SerpstatKeywordProcedure.getKeywordTop", **params))
    assert response.status_code == 200, response.text
    assert response.content == body  # the relay stays faithful: the envelope is read, never rewritten
    assert await _balance(clients) == before - credits * SERPSTAT_CREDIT
    telemetry = await _telemetry(clients)
    assert telemetry["cost_estimated_micro"] == estimate_credits * SERPSTAT_CREDIT
    assert telemetry["cost_observed_micro"] == credits * SERPSTAT_CREDIT
    assert telemetry["cost_charged_micro"] == credits * SERPSTAT_CREDIT


async def test_serpstat_ranked_keywords_reserves_size_and_settles_rows(
    clients: AsyncClient, serpstat_platform_on, monkeypatch,
):
    """`unit: keyword` had put this route on the per-input path: `size=100` reserved ONE credit
    and, with no settle rule, charged one credit for a 100-row page."""
    body = json.dumps({"id": "1", "result": {"data": _serp_rows(3), "summary_info": {"left_lines": 9}}}).encode()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, body))
    before = await _balance(clients)
    response = await clients.post("/call/serpstat.google.domain.ranked_keywords", json=_rpc(
        "SerpstatDomainProcedure.getDomainKeywords", domain="a.example", se="g_us", size=100))
    assert response.status_code == 200, response.text
    assert await _balance(clients) == before - 3 * SERPSTAT_CREDIT
    telemetry = await _telemetry(clients)
    assert telemetry["cost_estimated_micro"] == 100 * SERPSTAT_CREDIT
    assert telemetry["cost_charged_micro"] == 3 * SERPSTAT_CREDIT


@pytest.mark.parametrize(("envelope", "rows"), [
    ({"id": "1", "error": {"code": -32000, "message": "x"}}, 0),
    ({"id": "1", "error": "Invalid token"}, 0),
    ({"id": "1", "result": {"data": [{}, {}, {}], "summary_info": {"left_lines": 1}}}, 3),
    ({"id": "1", "result": {"data": {"top": [{}, {}]}, "summary_info": {}}}, 2),
    ({"id": "1", "result": {"data": {"top": []}}}, 1),
    ({"id": "1", "result": {"data": []}}, 1),
    ({"id": "1", "result": {"data": {}}}, 1),
    # keyed by the input, under `data` or directly under `result`; `summary_info` is never a row
    ({"id": "1", "result": {"data": {"seo": {"cost": 1}, "sem": {"cost": 2}}, "summary_info": {"left_lines": 1}}}, 2),
    ({"id": "1", "result": {"a.example": {"visible": 1}, "b.example": {"visible": 2}, "summary_info": {"left_lines": 1}}}, 2),
    ({"id": "1", "result": [{}, {}]}, 2),
])
def test_serpstat_settle_counts_envelope_rows(envelope, rows):
    mk = _mk("serpstat", endpoint_id=SERP_EP, cost_type="per_result", unit_micro=SERPSTAT_CREDIT)
    assert call_settle._observed_cost_micro(mk, json.dumps(envelope).encode()) == rows * SERPSTAT_CREDIT


@pytest.mark.parametrize("body", [
    b"{}", b"[]", b"not json", b'{"result": null}', b'{"result": "x"}', b'{"result": {"data": "x"}}',
    b'{"result": {"data": 7}}', b'{"error": null, "result": 3}',
])
def test_serpstat_unknown_envelopes_keep_the_estimate(body):
    mk = _mk("serpstat", endpoint_id=SERP_EP, cost_type="per_result", unit_micro=SERPSTAT_CREDIT)
    assert call_settle._observed_cost_micro(mk, body) is None


def test_serpstat_row_count_does_not_apply_to_flat_routes():
    """backlinks.summary is 5 credits per CALL and answers one object under `data`."""
    mk = _mk("serpstat", endpoint_id="serpstat.web.backlinks.summary", cost_type="per_call", unit_micro=5 * SERPSTAT_CREDIT)
    assert call_settle._observed_cost_micro(mk, b'{"id": "1", "result": {"data": {"referring_domains": 3}}}') is None
# ---------------------------------------------------------------------------------------------
# 2026-09-09: SE Ranking keyword ideas bill per keyword RETURNED, not per seed keyword

SERANKING_IDEAS = "seranking.google.keywords.ideas"
SERANKING_ROW_MICRO = 1_790  # 10 credits × $0.000179 (fx.yaml) per returned keyword


def _seranking_ideas_body(count: int) -> bytes:
    return json.dumps({"total": 3372, "keywords": [
        {"keyword": f"avocado idea {i}", "volume": 100 + i, "cpc": 0.03} for i in range(count)
    ]}).encode()


def test_seranking_ideas_catalog_prices_per_returned_row():
    """The catalog fact the fix rests on: the route is `unit: row` with the API's 100-row default,
    while the per-INPUT sibling keeps `unit: keyword` (its reserve is its bill)."""
    cat = catalog_store.load()
    ideas = cat.by_id[SERANKING_IDEAS]["cost"]
    assert (ideas["unit"], ideas["page_default"]) == ("row", 100)
    assert cat.by_id["seranking.google.keywords.volume"]["cost"]["unit"] == "keyword"
    assert call_resolution._usd_to_micro(cat.cost_view(ideas, "seranking")["usd"]) == SERANKING_ROW_MICRO


def test_platform_estimate_reserves_the_requested_rows_or_the_provider_page():
    """Row-priced: `limit` rows; with no limit, the catalog's own `page_default` (the API's 100),
    capped at the platform max; an absent or malformed page_default keeps the 20-row default."""
    est = call_resolution._platform_estimate_micro
    per_row = {"type": "per_result", "unit": "row", "usd": 0.00179, "page_default": 100}
    assert est(per_row, {"keyword": "avocado", "limit": "5"}) == 5 * SERANKING_ROW_MICRO
    assert est(per_row, {"keyword": "avocado"}) == 100 * SERANKING_ROW_MICRO
    assert est(per_row, {"keyword": "avocado", "limit": "500"}) == 100 * SERANKING_ROW_MICRO
    assert est({**per_row, "page_default": 250}, {}) == 100 * SERANKING_ROW_MICRO, "capped at the platform max"
    for bad in (None, 0, -1, True, "100", 1.5):
        assert est({**per_row, "page_default": bad}, {}) == 20 * SERANKING_ROW_MICRO
    # the per-INPUT sibling still counts the keywords the caller SENT, whatever limit says
    per_kw = {"type": "per_result", "unit": "keyword", "usd": 0.00179}
    assert est(per_kw, {"source": "us", "limit": "5"}, b'{"keywords": ["a", "b"]}') == 2 * SERANKING_ROW_MICRO


@pytest.mark.parametrize(("query", "count", "reserved_rows", "charged_rows"), [
    ("&limit=5", 5, 5, 5),        # the live case: 5 keywords returned cost 50 credits, treg took 10
    ("&limit=5", 0, 5, 0),        # the live case: an empty answer cost 0, treg took 10
    ("&limit=10", 3, 10, 3),      # fewer rows than asked settle at the rows
    ("&limit=5", 7, 5, 7),        # more rows than asked is an overrun the settle trues up
    ("", 100, 100, 100),          # no limit: the API answers (and bills) its 100-row default
])
async def test_seranking_ideas_settles_on_returned_keywords(
    clients: AsyncClient, monkeypatch, query, count, reserved_rows, charged_rows,
):
    """Reproduces the 2026-09-09 finding through the platform tier: every call used to reserve and
    settle ONE keyword unit ($0.00179) because the route was priced per input keyword. Now the hold
    is `limit` rows and the charge is the returned `keywords` list, in integer micro-USD."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_SERANKING", "SYNTHETIC-SERANKING-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "seranking")
    get_settings.cache_clear()
    body = _seranking_ideas_body(count)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, body))
    before = await _balance(clients)
    r = await clients.get(f"/call/{SERANKING_IDEAS}?source=us&keyword=avocado{query}")
    assert r.status_code == 200, r.text
    assert r.content == body, "the relay stays faithful: counting rows never rewrites the answer"
    assert await _balance(clients) == before - charged_rows * SERANKING_ROW_MICRO
    telemetry = await _telemetry(clients)
    assert telemetry["cost_estimated_micro"] == reserved_rows * SERANKING_ROW_MICRO
    assert telemetry["cost_observed_micro"] == charged_rows * SERANKING_ROW_MICRO
    assert telemetry["cost_charged_micro"] == charged_rows * SERANKING_ROW_MICRO
    get_settings.cache_clear()


async def test_seranking_ideas_unparseable_body_settles_at_the_estimate(clients: AsyncClient, monkeypatch):
    """A 2xx whose body is not JSON carries no row count: the hold settles at what was reserved."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_SERANKING", "SYNTHETIC-SERANKING-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "seranking")
    get_settings.cache_clear()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b"<html>gateway</html>"))
    before = await _balance(clients)
    r = await clients.get(f"/call/{SERANKING_IDEAS}?source=us&keyword=avocado&limit=5")
    assert r.status_code == 200
    assert await _balance(clients) == before - 5 * SERANKING_ROW_MICRO
    telemetry = await _telemetry(clients)
    assert telemetry["cost_observed_micro"] is None
    assert telemetry["cost_charged_micro"] == 5 * SERANKING_ROW_MICRO
    get_settings.cache_clear()


def test_seranking_ideas_observed_cost_counts_the_keywords_list():
    mk = _mk("seranking", endpoint_id=SERANKING_IDEAS, cost_type="per_result", unit_micro=SERANKING_ROW_MICRO)
    assert call_settle._observed_cost_micro(mk, _seranking_ideas_body(5)) == 5 * SERANKING_ROW_MICRO
    assert call_settle._observed_cost_micro(mk, _seranking_ideas_body(0)) == 0
    assert call_settle._observed_cost_micro(mk, b'{"keywords": [null, {"keyword": "x"}]}') == SERANKING_ROW_MICRO
    # an envelope with no rows (an error shape on a 2xx) costs nothing: pay-per-row
    assert call_settle._observed_cost_micro(mk, b'{"error": "bad source"}') == 0
    assert call_settle._observed_cost_micro(mk, b'{"keywords": null}') == 0
    # no JSON object, no count: the estimate stands
    for body in (b"not json", b"[1, 2, 3]", b""):
        assert call_settle._observed_cost_micro(mk, body) is None
    # the rule is keyed on the route, not the provider: the per-INPUT sibling and the row-priced
    # backlink lists keep settling at their reserve
    for other in ("seranking.google.keywords.volume", "seranking.web.backlinks.list"):
        sibling = _mk("seranking", endpoint_id=other, cost_type="per_result", unit_micro=SERANKING_ROW_MICRO)
        assert call_settle._observed_cost_micro(sibling, b'{"keywords": []}') is None
# ---- apify: the run-sync response IS the dataset, and the actor bills per item -----------------

def test_body_limit_reads_apify_max_items_and_results_limit():
    """Apify actors take their per-query cap as `maxItems` in the input body (`resultsLimit` for the
    Facebook actor). Neither was a limit signal, so every job search reserved the 20-row page and,
    with nothing to settle on, charged it: 3,019 calls at a flat $0.02 between 2026-08-20 and
    2026-09-09. `maxItems: 0` means "every page" for the actor and must keep the page default."""
    assert call_resolution._body_limit(json.dumps({"jobTitles": ["attorney"], "maxItems": 5}).encode()) == 5
    assert call_resolution._body_limit(json.dumps({"startUrls": [{"url": "https://x.example"}], "resultsLimit": 3}).encode()) == 3
    assert call_resolution._body_limit(json.dumps({"jobTitles": ["attorney"], "maxItems": 0}).encode()) is None
    assert call_resolution._body_limit(json.dumps({"jobTitles": ["attorney"], "maxItems": True}).encode()) is None
    assert call_resolution._body_limit(json.dumps({"jobTitles": ["attorney"], "maxItems": "5"}).encode()) is None


def test_apify_per_result_estimate_reserves_the_requested_items():
    """A `maxItems: 5` body on a $0.001/item actor reserves 5 rows, and the query-string cap counts
    the same way; without either signal the estimate stays the 20-row page."""
    cost = {"type": "per_result", "usd": 0.001}
    assert call_resolution._platform_estimate_micro(cost, {}, json.dumps({"jobTitles": ["x"], "maxItems": 5}).encode()) == 5_000
    assert call_resolution._platform_estimate_micro(cost, {"maxItems": "5"}, json.dumps({"jobTitles": ["x"]}).encode()) == 5_000
    assert call_resolution._platform_estimate_micro(cost, {"maxItems": "0"}, json.dumps({"jobTitles": ["x"]}).encode()) == 20_000
    assert call_resolution._platform_estimate_micro(cost, {}, json.dumps({"jobTitles": ["x"]}).encode()) == 20_000


@pytest.mark.parametrize(("body", "micro"), [
    (json.dumps([{"id": "1", "title": "Attorney"}]).encode(), 1_000),
    (json.dumps([{"id": str(i)} for i in range(30)]).encode(), 30_000),
    (b"[]", 0),
])
def test_apify_per_result_settles_on_the_items_delivered(body, micro):
    mk = _mk("apify", endpoint_id="apify.linkedin.search.jobs", cost_type="per_result", unit_micro=1_000)
    assert call_settle._observed_cost_micro(mk, body) == micro


@pytest.mark.parametrize("body", [
    b"not json", b'[{"id": "1"}, {"id": "2"', b"\x1f\x8b\x08\x00garbage", b'{"error": {"type": "run-failed"}}', b"42",
])
def test_apify_unknown_shapes_settle_at_the_estimate(body):
    """Gzip, a body the metered buffer truncated mid-array, or an envelope we did not expect: the
    count is unknown, so the estimate stands rather than a guess."""
    mk = _mk("apify", endpoint_id="apify.linkedin.search.jobs", cost_type="per_result", unit_micro=1_000)
    assert call_settle._observed_cost_micro(mk, body) is None


def test_apify_item_count_does_not_apply_outside_per_result():
    assert call_settle._observed_cost_micro(_mk("apify", cost_type="per_call", unit_micro=1_000), b"[1, 2]") is None
    assert call_settle._observed_cost_micro(_mk("apify", cost_type="per_result", unit_micro=0), b"[1, 2]") is None


@pytest.mark.parametrize(("query", "body_extra", "items", "estimate", "charged"), [
    ("", {}, 1, 20_000, 1_000),          # the old flat $0.02: no cap read, one job answered
    ("", {"maxItems": 5}, 3, 5_000, 3_000),
    ("?maxItems=5&maxTotalChargeUsd=0.05", {"maxItems": 5}, 30, 5_000, 30_000),  # overrun: settle bills what came back
    ("", {"maxItems": 5}, 0, 5_000, 0),
])
async def test_apify_job_search_reserves_the_cap_and_settles_on_the_array(
    clients: AsyncClient, platform_on, monkeypatch, query, body_extra, items, estimate, charged,
):
    """End to end through the real catalog row ($0.001 per job): the estimate follows `maxItems`
    and the settle follows the dataset array, so a one-job answer no longer costs the 20-row page."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_APIFY", "PLATFORM-APIFY-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "apify")
    get_settings.cache_clear()
    upstream = json.dumps([{"id": str(i), "title": "Attorney"} for i in range(items)]).encode()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, upstream))
    before = await _balance(clients)
    response = await clients.post(f"/call/apify.linkedin.search.jobs{query}",
                                  json={"jobTitles": ["attorney"], "postedLimit": "month", **body_extra})
    assert response.status_code == 200
    assert response.content == upstream
    assert await _balance(clients) == before - charged
    telemetry = await _telemetry(clients)
    assert telemetry["cost_estimated_micro"] == estimate
    assert telemetry["cost_observed_micro"] == charged
    assert telemetry["cost_charged_micro"] == charged
