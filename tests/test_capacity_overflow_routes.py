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
