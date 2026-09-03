"""Step B′ of docs/PROVIDER-CAPACITY-PLAN.md — overflow routes, the signature table and the
aggregator envelopes. Fixtures are recorded aggregator bodies (PII hashed) from the 2026-08-26
mapping run. No call-path involvement."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from treg.infra.db import reset_db, session_maker
from treg.domain.capacity import routes as R
from treg.domain.capacity import signatures as S
from treg.domain.capacity import verify as V
from treg.domain.capacity.policy import ensure_policies
from treg.domain.catalog import store as catalog_store
from treg.infra.upstream.aggregators import by_name, monid, orthogonal
from treg.models import CapacityPolicy, OverflowRoute
from treg.timeutil import utcnow_naive
from treg import worker

FIX = Path(__file__).parent / "fixtures" / "aggregators"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _route(**kw) -> OverflowRoute:
    base = dict(endpoint_id="findymail.search.business-profile", aggregator="orthogonal", provider="findymail",
                method="POST", path="/search/business-profile", agg_slug="findymail",
                agg_path="/api/search/business-profile", agg_price_micro=70_000, agg_unit="call", ratio=3.54,
                last_verified_at=utcnow_naive())
    base.update(kw)
    return OverflowRoute(**base)


# ---- rules ------------------------------------------------------------------------------------

def test_unit_kind_and_event_price_treat_hunters_per_ten_as_a_call():
    per10 = {"type": "per_result", "value": 1, "currency": "credit", "per": 10, "unit": "record"}
    assert R.our_unit_kind(per10) == "call"
    assert R.our_event_usd({**per10, "usd": 0.00245}) == pytest.approx(0.0245)
    assert R.our_unit_kind({"type": "per_result", "per": 1}) == "result"
    assert R.our_unit_kind({"type": "per_success"}) == "call"
    assert R.price_ratio(0.01, 0.0245) == pytest.approx(0.4082, abs=1e-4)
    assert R.price_ratio(0.01, None) is None


def test_eligibility_rule_in_order():
    cost = {"type": "per_success", "value": 0.0198, "currency": "USD"}
    ok = R.eligible(_route(), our_cost=cost, platform_eligible=True, policy=None)
    assert ok.enabled
    assert not R.eligible(_route(ratio=6.5), our_cost=cost, platform_eligible=True, policy=None).enabled
    assert "ratio" in R.eligible(_route(ratio=6.5), our_cost=cost, platform_eligible=True, policy=None).reason
    assert "unit mismatch" in R.eligible(_route(agg_unit="result"), our_cost=cost, platform_eligible=True, policy=None).reason
    assert R.eligible(_route(agg_unit="result", single_result=True), our_cost=cost, platform_eligible=True, policy=None).enabled
    assert "never verified" in R.eligible(_route(last_verified_at=None), our_cost=cost, platform_eligible=True, policy=None).reason
    old = utcnow_naive() - timedelta(days=8)
    assert "older than 7 days" in R.eligible(_route(last_verified_at=old), our_cost=cost, platform_eligible=True, policy=None).reason
    assert "not platform-eligible" in R.eligible(_route(), our_cost=cost, platform_eligible=False, policy=None).reason
    pol = CapacityPolicy(provider="findymail", overflow_allowed=False)
    assert "disallows" in R.eligible(_route(), our_cost=cost, platform_eligible=True, policy=pol).reason
    assert "no price" in R.eligible(_route(ratio=None), our_cost=cost, platform_eligible=True, policy=None).reason
    free = {"type": "free", "value": 0, "currency": "USD"}
    assert R.eligible(_route(ratio=None, agg_price_micro=10_000), our_cost=free, platform_eligible=True, policy=None, our_usd=0).enabled
    assert "free for us" in R.eligible(_route(ratio=None, agg_price_micro=20_000), our_cost=free, platform_eligible=True, policy=None, our_usd=0).reason


def test_match_catalogs_by_exact_host_method_path_with_prefix_folding():
    ours = [{"endpoint_id": "hunter.people.email.find", "provider": "hunter", "method": "GET",
             "path": "/email-finder", "base_url": "https://api.hunter.io/v2"},
            {"endpoint_id": "pdl.people.enrich", "provider": "pdl", "method": "GET",
             "path": "/person/enrich", "base_url": "https://api.peopledatalabs.com/v5"}]
    orth = [{"slug": "hunter", "baseUrl": "https://api.hunter.io",
             "endpoints": [{"path": "/v2/email-finder", "method": "get", "price": "$0.01"},
                           {"path": "/v2/other", "method": "GET", "price": "$0.01"}]}]
    monid_eps = [{"provider": "peopledatalabs", "endpoint": "/v5/person/enrich",
                  "price": {"type": "PER_RESULT", "amount": {"value": 0.3, "currency": "USD"}}},
                 {"provider": "hunterio", "endpoint": "/email-finder",
                  "price": {"type": "PER_RESULT", "amount": {"value": 0.02392, "currency": "USD"}}}]
    rows = R.match_catalogs(ours, orthogonal_apis=orth, monid_endpoints=monid_eps,
                            monid_alias={"peopledatalabs": "pdl", "hunterio": "hunter"})
    got = {(r["endpoint_id"], r["aggregator"]): r for r in rows}
    assert got[("hunter.people.email.find", "orthogonal")]["agg_price_usd"] == 0.01
    assert got[("hunter.people.email.find", "monid")]["agg_unit"] == "result"
    assert got[("pdl.people.enrich", "monid")]["agg_slug"] == "peopledatalabs"
    assert ("pdl.people.enrich", "orthogonal") not in got
    assert R._usd("dynamic") is None and R._usd({"amount": {"value": 1, "currency": "EUR"}}) is None


# ---- sync from the seed ------------------------------------------------------------------------

async def test_sync_reproduces_the_verified_set_and_never_enables_a_bad_ratio(monkeypatch):
    await reset_db()
    seed = R.load_seed()
    verified = {(x["endpoint_id"], x["aggregator"]) for x in seed if x["verified_at"]}
    assert len(verified) == 145, "the 2026-08-26 verified set (131 ROUTE + 11 tomba + 2 phone + hunter domain-search)"
    # Freeze "now" at the mapping date so the seed's stamps are within the 7-day window.
    now = R._dt("2026-08-27T00:00:00")
    cat = catalog_store.load()
    async with session_maker() as db:
        await ensure_policies(db, has_key=lambda p: True)
        result = await R.apply_sync(db, seed, catalog=cat, now=now)
        await db.commit()
    assert result.rows == len(seed)
    async with session_maker() as db:
        rows = (await db.execute(select(OverflowRoute))).scalars().all()
    on = {(r.endpoint_id, r.aggregator) for r in rows if r.enabled}
    by = {(r.endpoint_id, r.aggregator): r for r in rows}
    # Every enabled row is verified; nothing unverified, over-ratio, unit-mismatched or policy-barred is on.
    assert on <= verified
    assert all((by[k].ratio is not None and by[k].ratio <= R.MAX_RATIO)
               or (by[k].ratio is None and by[k].agg_price_micro <= R.FREE_ROUTE_MAX_USD * 1_000_000) for k in on)
    assert not any(k[0].startswith(("scrapecreators.", "tikhub.")) for k in on)
    assert ("hunter.companies.emails", "orthogonal") in on, "§10 correction: per-10 credit compares as a call"
    assert ("findymail.search.business-profile", "orthogonal") in on  # ratio 3.54 ≤ 4, the #1 402 source
    # What was verified but is NOT on, and why — every reason is one the rule names.
    off = {k: by[k].disabled_reason for k in verified - on}
    allowed = ("ratio ", "unit mismatch", "endpoint not platform-eligible",
               "policy for scrapecreators disallows overflow", "no price on one side", "free for us")
    assert all(r.startswith(allowed) for r in off.values()), off
    # Recorded 2026-08-28: 113 on. The 32 verified-but-off are the per-result-vs-per-call unit
    # question (23, plan §7), not platform-eligible (7), a $0.50 aggregator price on a free route,
    # a 56× ratio, scrapecreators policy, and rows with no aggregator price.
    assert len(on) == 113, (len(on), sorted(off.items()))
    assert ("tomba.companies.emails.count", "orthogonal") in on  # free for us, 1¢ there, 155 402s/30d
    # a route with ratio 6.5 in the seed never enables, and a re-sync without it disables it
    bad = {**seed[0], "endpoint_id": "findymail.search.business-profile", "aggregator": "monid",
           "agg_price_usd": 0.0198 * 6.5, "agg_unit": "call", "verified_at": "2026-08-26"}
    async with session_maker() as db:
        await R.apply_sync(db, seed + [bad], catalog=cat, now=now)
        await db.commit()
        row = await db.get(OverflowRoute, ("findymail.search.business-profile", "monid"))
        assert not row.enabled and row.disabled_reason.startswith("ratio 6.5")
    async with session_maker() as db:
        r2 = await R.apply_sync(db, [x for x in seed if x["endpoint_id"] != "findymail.search.business-profile"],
                                catalog=cat, now=now)
        await db.commit()
        row = await db.get(OverflowRoute, ("findymail.search.business-profile", "orthogonal"))
        assert not row.enabled and row.disabled_reason == "not in the current sync"
    # seven days later, without a re-verify, everything switches off
    async with session_maker() as db:
        r3 = await R.apply_sync(db, seed, catalog=cat, now=now + timedelta(days=8))
        assert r3.enabled == 0


def test_route_for_orders_orthogonal_first():
    rs = [_route(aggregator="monid", enabled=True), _route(aggregator="orthogonal", enabled=True),
          _route(aggregator="orthogonal", endpoint_id="x", enabled=True), _route(aggregator="monid", enabled=False, endpoint_id="y")]
    assert [r.aggregator for r in R.route_for(rs, "findymail.search.business-profile")] == ["orthogonal", "monid"]
    assert R.route_for(rs, "y") == []


# ---- signatures / 429 classifier --------------------------------------------------------------

def test_signature_table_classifies_balance_quota_and_burst():
    assert S.classify("findymail", 402, {}, b'{"message":"Not enough credits"}').kind == "balance"
    assert S.classify("thecompaniesapi", 403, {}, b'{"code":"noCreditsRemaining"}').kind == "balance"
    assert S.classify("thecompaniesapi", 403, {}, b'{"code":"nocreditsremaining"}').kind == "balance", "rows match any case"
    assert S.classify("lusha", 400, {}, b"You have reached your credit limit").kind == "balance"
    assert S.classify("anyone", 402, {}, b"").kind == "balance"
    now = utcnow_naive().replace(hour=10)
    lusha = S.classify("lusha", 429, {}, b'{"message":"Daily search limit reached"}', now=now)
    assert lusha.kind == "quota" and lusha.resets_at == (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    hunter = S.classify("hunter", 429, {}, b"You reached the limit for the number of searches per billing period")
    assert hunter.kind == "quota" and hunter.resets_at is None
    burst = S.classify("leadsforge", 429, {"retry-after": "9", "x-ratelimit-limit": "120"}, b"")
    assert burst.kind == "burst" and burst.retry_after_s == 9 and not S.is_exhausting(burst)
    long = S.classify("someone", 429, {"retry-after": "3600"}, b"")
    assert long.kind == "quota" and long.resets_at is not None
    assert S.classify("scrapecreators", 429, {}, b"slow down").kind == "unknown"
    assert S.classify("hunter", 400, {}, b"wrong params") is None
    assert S.classify("hunter", 500, {}, b"") is None
    assert S.classify("hunter", 200, {}, b"") is None
    raw = ((b"Retry-After", b"2"),)
    assert S.classify("crustdata", 429, raw, b"").retry_after_s == 2
    assert S.is_exhausting(S.classify("findymail", 402, {}, b"x")) and not S.is_exhausting(None)


# Apollo API reference, "422 Unprocessable Entity": the body an empty credit pool gets (live 2026-09-01).
APOLLO_OUT_OF_CREDITS = b'{"error": "Insufficient credits. Please upgrade your plan."}'
# The SAME status for a caller's mistake - must never read as our account running dry.
APOLLO_VALIDATION = b'{"error": "Please provide at least one of: first_name, last_name, email"}'


def test_apollo_says_out_of_credits_with_a_422():
    sig = S.classify("apollo", 422, None, APOLLO_OUT_OF_CREDITS)
    assert sig.kind == "balance" and S.is_exhausting(sig) and sig.resets_at is None
    assert S.classify("apollo", 422, None, APOLLO_VALIDATION) is None
    # Rows are per provider: a 422 is a validation status almost everywhere else.
    assert S.classify("hunter", 422, None, APOLLO_OUT_OF_CREDITS).kind == "unrecorded"


def test_every_recorded_phrase_arms_the_tripwire():
    """Recording one vendor's wording must arm the tripwire for every other: each literal body
    phrase in `_TABLE` (the 429 rows carry period words, not capacity phrases) is in CAPACITY_PHRASES."""
    import re as _re
    for provider, status, pattern, kind in S._TABLE:
        if not pattern or status == 429 or _re.escape(pattern) != pattern:
            continue  # empty (the bare 402 row), a period word, or a regex we cannot use as a body
        sig = S.classify("someone-else", 400, None, pattern.encode())
        assert sig is not None and sig.kind == "unrecorded", f"{provider}'s phrase {pattern!r} does not arm the tripwire"


def test_an_unrecorded_vendor_phrase_is_a_tripwire_never_a_mark():
    """The next Apollo: a 4xx no row matched whose body still names credits/quota/balance. It is
    logged and counted (`capacity_signal=unrecorded`) and does nothing else."""
    sig = S.classify("someone", 403, None, b'{"message":"Your quota has been exceeded"}')
    assert sig.kind == "unrecorded" and sig.detail == "quota has been exceeded" and not S.is_exhausting(sig)
    # a phrase recorded for ONE vendor arms the tripwire for every other
    assert S.classify("someone", 400, None, b"Not enough credits").kind == "unrecorded"
    assert S.classify("someone", 422, None, b'{"error":"insufficient parameters: first_name"}') is None
    # echoes of a caller's own request: period words, path names, half-words
    assert S.classify("coingecko", 400, None, b'{"error":"invalid interval: daily"}') is None
    assert S.classify("exa", 400, None, b"unbalanced quotes in query") is None
    assert S.classify("tikhub", 400, None, b"unterminated quotation mark") is None
    assert S.classify("twelvedata", 400, None, b"/balance_sheet: symbol parameter is missing") is None
    assert S.classify("tikhub", 400, None, b'{"error":"field balance is required"}') is None
    assert S.classify("tikhub", 400, None, b"quota must be an integer") is None
    assert S.classify("someone", 403, None, b"Your API quota limit was reached").kind == "unrecorded"
    # a rowless vendor saying it with a 429 and no retry-after trips the same wire, not `unknown`
    sig = S.classify("serpstat", 429, None, b'{"error":"Your monthly quota has been exhausted"}')
    assert sig.kind == "unrecorded" and sig.detail == "quota has been exhausted"
    assert S.classify("scrapecreators", 429, None, b"slow down").kind == "unknown"
    assert S.classify("someone", 401, None, b'{"error":"insufficient credits"}') is None, "401 is the key"
    assert S.classify("someone", 404, None, b'{"error":"no balance found for id"}') is None
    assert S.classify("someone", 500, None, b'{"error":"balance service down"}') is None


# Platform providers whose out-of-credit answer nobody has recorded in `_TABLE` yet. An acknowledged
# gap, not a claim the vendor never runs dry: their 4xx trips `unrecorded` instead.
_UNRECORDED_SIGNATURE = {
    "apify", "aviato", "branddev", "brightdata", "coingecko", "coresignal", "crustdata", "dataforseo",
    "diffbot", "exa", "fiber-ai", "finnhub", "icypeas", "influencersclub", "justoneapi", "marketstack",
    "moz", "oceanio", "pdl", "scrapecreators", "seranking", "serpapi", "serpstat", "spyfu", "tiingo",
    "tikhub", "tomba", "twelvedata",
}


def test_every_platform_provider_has_a_recorded_or_acknowledged_signature():
    """The guard. A provider gains a `platform_key_*` slot → record how it says "out of credits"
    (a row in `_TABLE`) or add it above knowingly. Silence is how Apollo's 422 went unseen."""
    from treg.config import platform_setting_name
    from treg.domain.capacity.collectors import all_platform_providers
    from treg.domain.catalog import store as catalog_store
    # Slot spelling is not provider spelling (`fiber_ai` vs `fiber-ai`); rows match `mk.provider`,
    # the catalog id, so the guard must speak that or a row written to satisfy it never fires.
    settings = {"platform_key_" + s for s in all_platform_providers()}
    catalog_ids = {e["provider"] for e in catalog_store.load().endpoints}
    slots = {p for p in catalog_ids if platform_setting_name(p) in settings}
    assert len(slots) == len(settings), f"a key slot with no catalog provider: {sorted(settings - {platform_setting_name(p) for p in slots})}"
    recorded = {p for p, *_ in S._TABLE if p != "*"}
    missing = slots - recorded - _UNRECORDED_SIGNATURE
    assert not missing, f"record how these say 'out of credits' or acknowledge them: {sorted(missing)}"
    stale = (_UNRECORDED_SIGNATURE & recorded) | (_UNRECORDED_SIGNATURE - slots)
    assert not stale, f"acknowledged providers that are now recorded or gone: {sorted(stale)}"


def test_litescrape_payment_required_is_an_exhausted_balance():
    sig = S.classify(
        "litescrape",
        402,
        {"content-type": "application/json"},
        b'{"error":"The API key has no calls remaining.","error_code":"payment_required"}',
    )
    assert sig is not None and sig.kind == "balance" and S.is_exhausting(sig)


_CF_PAGE = (b"<!DOCTYPE html><html><head><title>Access denied | api.example.com used Cloudflare "
            b"to restrict access</title></head><body>Error 1010</body></html>")


def test_a_cdn_block_page_is_an_edge_block_not_an_account_signal():
    """A CDN refusing the request's shape says nothing about treg's account: never exhausting."""
    assert S.classify("anyone", 403, {"cf-mitigated": "challenge"}, b"").kind == "edge_block"
    assert S.classify("anyone", 503, {"cf-mitigated": "challenge"}, b"").kind == "edge_block"
    hdrs = ((b"Server", b"cloudflare"), (b"Content-Type", b"text/html; charset=UTF-8"), (b"CF-RAY", b"9x"))
    sig = S.classify("anyone", 403, hdrs, _CF_PAGE)
    assert sig.kind == "edge_block" and not S.is_exhausting(sig)
    # A JSON 403 from behind the same CDN is the vendor's own answer.
    assert S.classify("anyone", 403, {"server": "cloudflare", "content-type": "application/json"},
                      b'{"error":"too many requests"}') is None
    assert S.classify("thecompaniesapi", 403, {"server": "cloudflare", "content-type": "application/json"},
                      b'{"code":"noCreditsRemaining"}').kind == "balance"
    # An HTML 503 without the marker is the vendor down, not a block.
    assert S.classify("anyone", 503, {"server": "cloudflare", "content-type": "text/html"}, b"<html>") is None
    # The CDN's problem-JSON block (error 1010, "browser's signature"): no marker header, not HTML.
    cf_json = (b'{"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/'
               b'cloudflare-1xxx-errors/error-1010/","title":"Error 1010: Access denied","status":403}')
    sig = S.classify("anyone", 403, {"content-type": "application/problem+json"}, cf_json)
    assert sig.kind == "edge_block" and not S.is_exhausting(sig)


# ---- aggregator envelopes round-trip their fixtures --------------------------------------------

def test_orthogonal_build_wraps_the_vendor_request_unchanged():
    req = orthogonal.build(_route(), "K", {"limit": 1, "domain": "stripe.com"}, b'{"name":"Patrick"}',
                           {"id": "42"})
    assert req.url.endswith("/run") and req.headers["Authorization"] == "Bearer K"
    assert req.json == {"api": "findymail", "path": "/api/search/business-profile",
                        "query": {"limit": "1", "domain": "stripe.com"}, "body": {"name": "Patrick"}}
    req2 = orthogonal.build(_route(agg_path="/api/v1/{id}/x"), "K", [], None, {"id": "42"})
    assert req2.json == {"api": "findymail", "path": "/api/v1/42/x"}


def test_orthogonal_parse_success_price_upstream_error_and_contract():
    ok = _fixture("orthogonal_ok")
    res = orthogonal.parse(ok["status"], json.dumps(ok["body"]).encode())
    assert res.ok and res.upstream_status == 200 and res.cost_micro == ok["expect"]["price"] * 10_000
    assert json.loads(res.upstream_body) == ok["body"]["data"]  # the vendor body, verbatim
    rel = _fixture("orthogonal_upstream_402")
    res = orthogonal.parse(rel["status"], json.dumps(rel["body"]).encode())
    assert res.failure is None and res.upstream_status == 402 and not res.ok
    assert json.loads(res.upstream_body) == rel["body"]["data"]
    con = _fixture("orthogonal_contract_404")
    res = orthogonal.parse(con["status"], json.dumps(con["body"]).encode())
    assert res.upstream_status == 404 and res.failure is None  # the vendor DID answer here (data present)
    only_contract = {"success": False, "error": "x", "_orthogonal": {"error": "orthogonal_endpoint_contract", "message": "company required"}}
    res = orthogonal.parse(400, json.dumps(only_contract).encode())
    assert res.failure == "contract" and res.cost_micro == 0
    assert orthogonal.parse(401, b'{"error":"invalid key"}').failure == "aggregator_auth"
    assert orthogonal.parse(402, b'{"success":false,"error":"insufficient balance"}').failure == "aggregator_balance"
    assert orthogonal.parse(200, b"<html>").failure == "malformed"


def test_monid_build_and_parse_fixtures():
    r = _route(aggregator="monid", agg_slug="hunterio", agg_path="/domain-search", agg_unit="result")
    req = monid.build(r, "K", {"domain": "stripe.com", "limit": "1", "score": "0.5", "raw": "true", "id": "007a"}, None)
    assert req.json == {"provider": "hunterio", "endpoint": "/domain-search",
                        "input": {"queryParams": {"domain": "stripe.com", "limit": 1, "score": 0.5, "raw": True, "id": "007a"},
                                  "body": {}, "pathParams": {}}}, "Monid validates JSON types: numeric strings become numbers (live 2026-08-28)"
    alt = monid.build(r, "K", {"domain": "stripe.com"}, None, params_as_body=True)
    assert alt.json["input"] == {"queryParams": {}, "body": {"domain": "stripe.com"}, "pathParams": {}}
    ok = _fixture("monid_ok_sync")
    res = monid.parse(ok["status"], json.dumps(ok["body"]).encode())
    assert res.ok and res.upstream_status == 200 and res.cost_micro == 23_920
    assert json.loads(res.upstream_body) == ok["body"]["output"]
    miss = _fixture("monid_miss_zero_cost")
    res = monid.parse(miss["status"], json.dumps(miss["body"]).encode())
    assert res.ok and res.cost_micro == 0 and res.extra["result_count"] == 0
    bad = _fixture("monid_validator_400")
    res = monid.parse(bad["status"], json.dumps(bad["body"]).encode())
    assert res.failure == "contract" and "Invalid input" in res.detail and res.cost_micro == 0
    pend = _fixture("monid_async_202")
    first = {"runId": pend["body"]["runId"], "status": "RUNNING"}
    res = monid.parse(202, json.dumps(first).encode())
    assert res.failure == "pending" and res.poll_url.endswith(pend["body"]["runId"])
    done = monid.parse(200, json.dumps(pend["body"]).encode())
    assert done.ok and done.cost_micro == 12_000 and json.loads(done.upstream_body) == pend["body"]["output"]
    assert monid.parse(401, b"{}").failure == "aggregator_auth"
    assert monid.parse(402, b'{"message":"hit your account maximum"}').failure == "aggregator_balance"
    failed = {"runId": "r", "status": "FAILED", "message": "provider down", "providerResponse": {"httpStatus": 503}}
    res = monid.parse(200, json.dumps(failed).encode())
    assert res.failure is None and res.upstream_status == 503 and not res.ok
    assert by_name("monid") is monid and by_name("orthogonal") is orthogonal


def test_every_fixture_round_trips_through_its_adapter():
    for path in FIX.glob("*.json"):
        fx = json.loads(path.read_text())
        adapter = orthogonal if path.name.startswith("orthogonal") else monid
        res = adapter.parse(fx["status"], json.dumps(fx["body"]).encode())
        assert res.failure in (None, "contract", "pending"), (path.name, res.failure, res.detail)
        exp = fx["expect"]
        if exp.get("ok") is True:
            assert res.ok and res.upstream_body, path.name
        if exp.get("upstream_status") and res.failure is None:
            assert res.upstream_status == exp["upstream_status"], path.name


# ---- verification -----------------------------------------------------------------------------

def test_shape_fingerprint_ignores_values_but_not_structure():
    a = b'{"data":{"email":"a@x.io","score":90,"sources":[{"uri":"u"}]}}'
    b = b'{"data":{"email":"b@y.io","score":1,"sources":[{"uri":"v"},{"uri":"w"}]}}'
    c = b'{"data":{"email":"b@y.io"}}'
    assert V.shapes_match(a, b) is True and V.shapes_match(a, c) is False and V.shapes_match(a, b"nope") is None


def test_shape_empty_vs_nonempty_list_differs_but_both_empty_match():
    """Regression for leadsforge.people.enrich.job.list shape verification failure.

    The endpoint lists account-specific jobs. Direct (treg's Leadsforge account) vs relay
    (Orthogonal's Leadsforge account) returned different job histories: one had jobs, the
    other empty. `shape()` distinguishes `[]` from `[{...}]`, so verify failed.

    Fix: set `test_request.queryParams.from` to a far-future date, guaranteeing both
    accounts return empty lists → same shape → verify passes.
    """
    with_jobs = b'{"jobs": [{"jobID": "abc", "status": "completed"}], "offset": 0, "limit": 5, "total": 1}'
    no_jobs = b'{"jobs": [], "offset": 0, "limit": 5, "total": 0}'
    both_empty = b'{"jobs": [], "offset": 0, "limit": 5, "total": 0}'

    assert V.shapes_match(with_jobs, no_jobs) is False, "non-empty vs empty list shapes must differ"
    assert V.shapes_match(no_jobs, both_empty) is True, "both-empty lists must have same shape"
    assert V.shape({"jobs": []}) == {"jobs": []}
    assert V.shape({"jobs": [{"id": "x"}]}) == {"jobs": [{"id": "leaf"}]}


def _verification(**kw) -> V.Verification:
    base = dict(endpoint_id="e", aggregator="orthogonal", direct_status=200, relay_status=200, same_shape=True,
                cost_micro=1, verified_at=utcnow_naive(), note="")
    return V.Verification(**{**base, **kw})


def test_verdict_disables_only_a_route_that_is_actually_wrong():
    """The verifier must never switch off the routes that exist to cover OUR key running dry, nor
    every route behind an aggregator that is having a bad morning."""
    assert V.verdict(_verification()) == "passed"
    # nothing to compare with: our own account dry (the 2026-09-01 state), a 401, no vendor key,
    # the vendor host down, a stale test_request that fails both legs, an async run still pending
    assert V.verdict(_verification(direct_status=422, same_shape=None, verified_at=None, direct_dry=True)) == "inconclusive"
    assert V.verdict(_verification(direct_status=401, same_shape=None, verified_at=None)) == "inconclusive"
    assert V.verdict(_verification(direct_status=None, same_shape=None, verified_at=None)) == "inconclusive"
    assert V.verdict(_verification(direct_status=None, relay_status=400, same_shape=None, verified_at=None)) == "inconclusive"
    assert V.verdict(_verification(direct_status=401, relay_status=400, same_shape=None, verified_at=None)) == "inconclusive"
    assert V.verdict(_verification(direct_status=422, relay_status=404, same_shape=None, verified_at=None, direct_dry=True)) == "inconclusive"
    assert V.verdict(_verification(relay_status=None, same_shape=None, verified_at=None, failure="pending")) == "inconclusive"
    # the aggregator's side: key, account, host, envelope, its vendor pool
    for failure in ("aggregator_auth", "aggregator_balance", "malformed", "unreachable", "vendor_dry"):
        assert V.verdict(_verification(direct_status=None, relay_status=None, same_shape=None, verified_at=None,
                                       failure=failure)) == "aggregator", failure
    # this route is shown wrong: the direct leg proves the request, the relay does not match it
    assert V.verdict(_verification(same_shape=False, verified_at=None)) == "failed"
    assert V.verdict(_verification(relay_status=None, same_shape=None, verified_at=None, failure="contract")) == "failed"
    assert V.verdict(_verification(relay_status=500, same_shape=None, verified_at=None)) == "failed"
    assert V.verdict(_verification(relay_status=404, same_shape=None, verified_at=None)) == "failed"


async def test_verify_route_with_our_own_key_dry_is_inconclusive_not_a_failure():
    import httpx
    r = _route(aggregator="orthogonal", agg_slug="apollo", agg_path="/people/match", method="POST", path="/people/match",
               provider="apollo")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.orthogonal.dev" or "/run" in request.url.path:
            return httpx.Response(200, json={"success": True, "data": {"person": {"id": "p"}}, "priceCents": 1.0})
        return httpx.Response(422, json={"error": "Insufficient credits. Please upgrade your plan."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        v = await V.verify_route(c, r, key="K", direct=("https://api.apollo.io/api/v1/people/match", {}, b"{}"),
                                 test_request={"body": {"email": "x@y.z"}}, direct_headers={})
    assert not v.passed and v.direct_status == 422 and v.relay_status == 200
    assert v.direct_dry and v.relay_ok, "our account, in Apollo's dialect - the worker still stamps this"
    assert V.verdict(v) == "inconclusive", "the route still works; it is our account that is dry"


async def test_verify_route_reads_a_relayed_out_of_credits_answer_as_the_aggregators_dry_account():
    import httpx
    r = _route(aggregator="orthogonal", agg_slug="apollo", agg_path="/people/match", method="POST", path="/people/match",
               provider="apollo")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/run" in request.url.path:  # Orthogonal relays Apollo's 422 with its body
            return httpx.Response(422, json={"success": False, "error": "Upstream returned status 422",
                                             "data": {"error": "Insufficient credits. Please upgrade your plan."}, "priceCents": 1.0})
        return httpx.Response(200, json={"person": {"id": "p"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        v = await V.verify_route(c, r, key="K", direct=("https://api.apollo.io/api/v1/people/match", {}, b"{}"),
                                 test_request={"body": {"email": "x@y.z"}}, direct_headers={})
    assert v.relay_status == 422 and v.failure == "vendor_dry" and v.note.startswith("vendor_dry: balance")
    assert V.verdict(v) == "aggregator", "Orthogonal's Apollo account is empty; the route is not wrong"


async def test_verify_route_marks_same_shape_and_polls_async_runs():
    import httpx
    r = _route(aggregator="monid", agg_slug="hunterio", agg_path="/email-finder", agg_unit="result",
               method="GET", path="/email-finder")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/run":
            return httpx.Response(202, json={"runId": "RUN1", "status": "RUNNING"})
        if request.url.path == "/v1/runs/RUN1":
            return httpx.Response(200, json={"runId": "RUN1", "status": "COMPLETED", "output": {"data": {"email": "x"}},
                                             "providerResponse": {"httpStatus": 200},
                                             "billing": {"reportedCost": {"value": 23920, "unit": "MICRO_DOLLAR"}}})
        return httpx.Response(200, json={"data": {"email": "y"}})  # the direct call

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        v = await V.verify_route(c, r, key="K", direct=("https://api.hunter.io/v2/email-finder", {"domain": "x"}, None),
                                 test_request={"queryParams": {"domain": "x"}}, direct_headers={})
    assert v.passed and v.cost_micro == 23_920 and v.verified_at is not None
    assert ("GET", "/v1/runs/RUN1") in calls
    # verify never polls forever on a stuck run
    async def stuck(request):
        return httpx.Response(202, json={"runId": "R", "status": "RUNNING"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(stuck)) as c:
        res = await V.relay_once(c, r, "K", {}, None, max_polls=2, poll_wait_s=0)
    assert res.failure == "pending"


def test_worker_cli_parses_overflow_commands(monkeypatch):
    seen = {}
    async def fake(args):
        seen.update(vars(args)); return 0
    monkeypatch.setattr(worker, "_overflow_sync", fake)
    monkeypatch.setattr(worker, "_overflow_verify", fake)
    assert worker.main(["overflow", "sync", "--live"]) == 0 and seen["live"] is True
    assert worker.main(["overflow", "verify", "--max-usd", "0.05"]) == 0 and seen["max_usd"] == 0.05
