"""Capability routing — first-party routed endpoints (docs/CAPABILITY-ROUTING-PLAN.md).
`treg.people.email.find` picks a child and runs it through the ordinary call use case."""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.domain import money as ledger
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import paths as P
from treg.domain.catalog.routing.contracts import canonical_identity
from treg.domain.catalog.routing.plan import Candidate, cost_at, rank
from treg.infra.catalog_observations import CachedEndpointObservationReader
from treg.models import CallRecord, Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401

ROUTED = "treg.people.email.find"


@pytest.fixture
def enrichment_on(monkeypatch, platform_on):
    for p in ("HUNTER", "TOMBA", "LEADMAGIC", "LEADSFORGE", "FINDYMAIL", "AVIATO", "FIBER_AI"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _relay_by_provider(answers: dict[str, list[tuple[int, dict]]], seen: list):
    """A fake upstream keyed by the vendor host: each provider answers its scripted list in order."""
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        provider = next((p for p in answers if p != "*" and p in upstream_url), "*")  # "*": any other vendor
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        seen.append((provider, request.method, dict(request.query_items), json.loads(body) if body else None))
        status, doc = answers[provider].pop(0)
        payload = json.dumps(doc).encode()
        async def _s():
            yield payload
        async def _c():
            return None
        return UpstreamResponse(status, ((b"content-type", b"application/json"),), _s(), _c)
    return _relay


# ---- pure ------------------------------------------------------------------------------------

def test_expression_language():
    doc = {"data": {"email": "a@x.io", "score": 80, "verification": {"status": "valid"}}, "emails": [{"email": "e", "type": "work"}], "none": []}
    assert P.evaluate("data.email", doc) == "a@x.io"
    assert P.evaluate("data.score / 100", doc) == 0.8
    assert P.evaluate("data.verification.status == 'valid'", doc) is True
    assert P.evaluate("data.email == null", doc) is False and P.evaluate("data.missing == null", doc) is True
    assert P.evaluate("none == []", doc) is True and P.evaluate("emails == []", doc) is False
    assert P.evaluate("emails[0].email", doc) == "e" and P.evaluate("emails[3].email", doc) is None
    assert P.evaluate("coalesce(data.missing, data.email)", doc) == "a@x.io"
    assert P.evaluate("split_first(data.name)", {"data": {"name": "Patrick Collison"}}) == "Patrick"
    assert P.evaluate("split_last(data.name)", {"data": {"name": "Patrick"}}) is None
    assert P.evaluate("join(a, b)", {"a": "Patrick", "b": "Collison"}) == "Patrick Collison"
    with pytest.raises(ValueError):
        P.evaluate("nope(a)", doc)


def test_every_shipped_adapter_round_trips_its_fixture():
    cat = catalog_store.load()
    bad = {eid: a.verify_note for eid, a in cat.adapters.items() if not a.verified}
    assert bad == {}, bad
    ep = cat.by_id[ROUTED]
    assert ep["kind"] == "routed" and ep["provider"] == "treg" and len(ep["routed_children"]) >= 8
    assert cat.platform_eligible(ep) and ep["cost_range_usd"][0] < ep["cost_range_usd"][1]
    # a hand-verified round trip on the plan's worked example
    ad = cat.adapters["leadsforge.people.email.find"]
    q, b = ad.to_upstream({"first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com", "full_name": "Patrick Collison"})
    assert b == {"firstName": "Patrick", "lastName": "Collison", "companyDomain": "stripe.com"} and q == {}
    assert ad.from_upstream({"email": "p@stripe.com", "status": "succeeded"}) == {"email": "p@stripe.com", "verified": True}
    assert ad.is_miss({"email": None}) and not ad.is_miss({"email": "x"})


def test_identity_variants_derive_and_never_cross():
    contract = catalog_store.load().contracts["people.email.find"]
    ident, variant = canonical_identity(contract, {"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert variant == ("domain", "full_name") and ident["first_name"] == "Patrick" and ident["last_name"] == "Collison"
    ident, variant = canonical_identity(contract, {"first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
    assert ident["full_name"] == "Patrick Collison"
    ident, variant = canonical_identity(contract, {"linkedin_url": "https://www.linkedin.com/in/x"})
    assert variant == ("linkedin_url",) and "domain" not in ident
    assert canonical_identity(contract, {"full_name": "Patrick Collison"})[1] is None


def test_cost_at_and_ranking_math():
    assert cost_at({"usd": 0.0038, "type": "per_result", "per": 1}, {"limit": 10}) == 38_000
    assert cost_at({"usd": 0.0044, "type": "per_result", "per": 25}, {"limit": 10}) == 110_000, "lusha: 1 credit per 25 rows, minimum 1"
    assert cost_at({"usd": 0.0044, "type": "per_result", "per": 25}, {"limit": 40}) == 220_000
    assert cost_at({"usd": 0.005, "type": "per_call"}, {"limit": 10}) == 5_000
    assert cost_at({"usd": None}, {}) is None
    ep = lambda i, t="per_success": {"id": i, "provider": i.split(".")[0], "cost": {"type": t}}
    a = Candidate(ep("a.x"), None, ("domain",), "platform", 24_500, hit_rate=0.4, ok_rate=None, p50_ms=100, last_ok_days=1)
    b = Candidate(ep("b.x", "per_call"), None, ("domain",), "platform", 20_000, hit_rate=0.8, ok_rate=None, p50_ms=100, last_ok_days=1)
    own = Candidate(ep("c.x"), None, ("domain",), "credential", 0, hit_rate=None, ok_rate=None, p50_ms=None, last_ok_days=None)
    assert a.expected_cost_per_hit == pytest.approx(24_500), "per-success: billed only on a hit → price per hit"
    assert b.expected_cost_per_hit == pytest.approx(25_000), "per-call at 80% hit rate: 20000/0.8"
    assert [c.endpoint["id"] for c in rank([a, b, own])] == ["c.x", "a.x", "b.x"]
    assert [c.endpoint["id"] for c in rank([a, b], prefer=["b"])] == ["b.x", "a.x"]
    assert [c.endpoint["id"] for c in rank([a, b], exclude=["a"])] == ["b.x"]
    a.exhausted = True
    assert [c.endpoint["id"] for c in rank([a, b])] == ["b.x"]


async def test_concurrent_routed_plans_share_one_cached_observation_refresh(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    from treg.domain.catalog import stats

    class Source:
        calls = 0

        async def get_many(self, endpoint_ids):
            self.calls += 1
            return {
                endpoint_id: {
                    "samples": 20, "ok_rate": 1.0, "p50_ms": 20, "p95_ms": 40,
                    "last_ok_days": 0, "hit_rate": 0.5, "hit_samples": 20,
                }
                for endpoint_id in endpoint_ids
            }

    async def request_time_aggregate(*args, **kwargs):
        raise AssertionError("routed planning must not aggregate CallRecord on the request path")

    source = Source()
    reader = CachedEndpointObservationReader(source)
    monkeypatch.setattr(call_route, "_endpoint_observation_reader", reader, raising=False)
    monkeypatch.setattr(stats, "observed", request_time_aggregate)
    ep = catalog_store.load().by_id[ROUTED]

    class _Org:
        id = 1

    class _Caller:
        org_id = 1
        org = _Org()

    try:
        plans = await asyncio.gather(*(
            call_route.build_plan(
                ep, {"full_name": "Patrick Collison", "domain": "stripe.com"},
                _Caller(), call_route.RouteOptions.from_headers(lambda key: None),
            )
            for _ in range(20)
        ))
        await reader.wait_for_idle()
    finally:
        await reader.aclose()

    assert all(plan.candidates for plan in plans)
    assert source.calls == 1


async def test_routed_plan_keeps_per_success_hit_fallback_from_the_cache(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    from treg import api as A
    from treg.domain.catalog import stats

    endpoint_id = "tomba.people.email.find"
    async with session_maker() as db:
        for cost_micro in (8_900, 8_900, 0):
            db.add(CallRecord(
                org_id=1, user_email="a@b.c", tool_name=endpoint_id, method="GET", path="/x",
                status_code=200, endpoint_id=endpoint_id, cost_observed_micro=cost_micro, hit=None,
            ))
        await db.commit()
    monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
    reader = A.app.state.endpoint_observation_reader
    assert await reader.get_many([endpoint_id]) == {}
    await reader.wait_for_idle()
    warm = await reader.get_many([endpoint_id])
    assert warm[endpoint_id]["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)

    async def request_time_aggregate(*args, **kwargs):
        raise AssertionError("the routed plan must use the warm observation cache")

    monkeypatch.setattr(stats, "observed", request_time_aggregate)
    monkeypatch.setattr(call_route, "_endpoint_observation_reader", reader, raising=False)
    ep = catalog_store.load().by_id[ROUTED]

    class _Org:
        id = 1

    class _Caller:
        org_id = 1
        org = _Org()

    plan = await call_route.build_plan(
        ep, {"full_name": "Patrick Collison", "domain": "stripe.com"},
        _Caller(), call_route.RouteOptions.from_headers(lambda key: None),
    )
    tomba = next(candidate for candidate in plan.candidates if candidate.endpoint["id"] == endpoint_id)
    assert tomba.hit_rate == pytest.approx(2 / 3, abs=1e-3)


# ---- the call path ---------------------------------------------------------------------------

async def test_routed_call_runs_the_cheapest_child_and_returns_output_raw_and_provenance(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "patrick@stripe.com", "score": 99, "first_name": "Patrick", "last_name": "Collison",
                                    "verification": {"status": "valid"}}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"] == {"email": "patrick@stripe.com", "confidence": 0.99, "first_name": "Patrick", "last_name": "Collison", "verified": True}
    assert d["raw"]["data"]["score"] == 99, "the winning provider's body, verbatim"
    assert d["_treg"]["served_by"] == "tomba.people.email.find" and d["_treg"]["outcome"] == "hit"
    assert r.headers["X-Treg-Served-By"] == "tomba.people.email.find" and r.headers["X-Treg-Providers-Tried"] == "tomba"
    assert seen == [("tomba", "GET", {"domain": "stripe.com", "full_name": "Patrick Collison"}, None)]
    charged = int(r.headers["X-Treg-Cost-Micro"])
    assert charged == 8_900 and before - await _balance(clients) == charged, "tomba's price, nothing else"
    assert d["_treg"]["charged_micro"] == charged
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    assert {e.call_id for e in entries if e.kind == "settle"} == {r.headers["X-Treg-Call-Id"] + ":r0"}
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    kinds = {(x["tool_name"], x.get("credential_tier")) for x in rows}
    assert (ROUTED, "routed") in kinds and ("tomba.people.email.find", "platform") in kinds


async def test_error_on_the_first_child_falls_back_to_the_second(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(503, {"error": "down"})],
         "findymail": [(200, {"contact": {"name": "Patrick Collison", "email": "patrick@stripe.com"}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["error", "hit"]
    assert d["_treg"]["served_by"] == "findymail.search.name" and d["output"]["email"] == "patrick@stripe.com"
    assert r.headers["X-Treg-Providers-Tried"] == "tomba,findymail"
    assert before - await _balance(clients) == 19_800, "the failed child released its hold; only findymail charged"
    assert seen[1] == ("findymail", "POST", {}, {"name": "Patrick Collison", "domain": "stripe.com"})


async def test_child_capability_pin_refusal_falls_back_to_the_pinned_provider(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    pinned = await clients.post(
        f"/orgs/{org_id}/pins",
        json={"capability": "people.email.find", "provider": "hunter"},
    )
    assert pinned.status_code == 200, pinned.text
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "hunter": [(200, {"data": {
            "email": "patrick@stripe.com", "score": 90,
            "verification": {"status": "valid"},
        }})],
    }, seen))

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,hunter"},
    )

    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["_treg"]["served_by"] == "hunter.people.email.find"
    assert [attempt["outcome"] for attempt in doc["_treg"]["tried"]] == ["error", "hit"]
    assert doc["_treg"]["tried"][0]["endpoint_id"] == "tomba.people.email.find"
    assert [provider for provider, *_ in seen] == ["hunter"]


async def test_platform_vendor_401_falls_back_to_the_next_provider(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    findymail = catalog_store.load().by_id["findymail.search.name"]
    monkeypatch.setitem(findymail, "cost", {**findymail["cost"], "type": "per_call"})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [(401, {"error": "invalid platform key"})],
        "findymail": [(200, {"contact": {
            "name": "Patrick Collison", "email": "patrick@stripe.com",
        }})],
    }, seen))

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={
            "X-Treg-Route-Prefer": "tomba,findymail",
            "X-Treg-Route-Exclude": "hunter,leadmagic,leadsforge,aviato,fiber-ai",
        },
    )

    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["_treg"]["served_by"] == "findymail.search.name"
    assert [attempt["outcome"] for attempt in doc["_treg"]["tried"]] == ["error", "hit"]
    assert [provider for provider, *_ in seen] == ["tomba", "findymail"]


async def test_routed_insufficient_balance_still_stops_before_fallback(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "drain routed balance", 1_000_000)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [(200, {"data": {"email": "should-not-run@example.com"}})],
    }, seen))

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,findymail"},
    )

    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "insufficient_balance"
    assert seen == []


async def test_waterfall_is_on_by_default_can_be_turned_off_and_respects_max_cost(clients: AsyncClient, enrichment_on, monkeypatch):
    miss_tomba = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [miss_tomba]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss" and r.json()["output"]["email"] is None
    assert r.headers["X-Treg-Route-Outcome"] == "miss" and len(seen) == 1, "waterfall off: stop at the first miss"
    # waterfall (the default): miss → next cheapest → hit; skips a candidate that would breach the ceiling
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [miss_tomba], "findymail": [(200, {"contact": {"name": "N H", "email": None}})],
         "hunter": [(200, {"data": {"email": "n@stripe.com", "score": 50, "verification": {"status": "valid"}}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Max-Cost": "0.08"})
    assert r.status_code == 200, r.text
    tried = r.json()["_treg"]["tried"]
    assert [t["outcome"] for t in tried] == ["miss", "miss", "hit"] and r.json()["_treg"]["served_by"] == "hunter.people.email.find"
    assert [p for p, *_ in seen] == ["tomba", "findymail", "hunter"]
    assert r.json()["_treg"]["charged_micro"] == 24_500, "misses on per-success providers are free; only the hit is billed"
    assert [t["charged_micro"] for t in tried] == [0, 0, 24_500]
    # a ceiling the third candidate would breach stops the waterfall there
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [miss_tomba], "findymail": [(200, {"contact": {"name": "N H", "email": None}})],
         "hunter": [(200, {"data": {"email": None, "score": None}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Max-Cost": "0.02"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    # free misses do not consume the ceiling, but hunter (2.45¢ > 2¢) and everything dearer is skipped
    assert [p for p, *_ in seen] == ["tomba", "findymail"]
    assert all(t["outcome"] in ("miss", "skipped") for t in r.json()["_treg"]["tried"]) and r.json()["_treg"]["charged_micro"] == 0


async def test_max_cost_below_the_cheapest_refuses_before_any_call(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [(200, {})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "P C", "domain": "stripe.com"}, headers={"X-Treg-Route-Max-Cost": "0.001"})
    assert r.status_code == 402 and r.json()["detail"]["error"] == "route_max_cost" and seen == []
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_identity_no_provider_accepts_is_422_naming_variants(clients: AsyncClient, enrichment_on):
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "identity_incomplete"
    assert ["domain", "full_name"] in r.json()["detail"]["variants"]


async def test_caller_fault_on_a_child_stops_and_own_key_ranks_first(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    await clients.post("/secrets", json={"name": "hunter", "value": "MY-HUNTER-KEY"})  # tier 2 for hunter
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "hunter.people.email.find"
    assert r.json()["_treg"]["tier"] == "credential" and await _balance(clients) == before, "own key: first, and free"
    # a vendor 4xx on the child goes on ONLY to providers that bill nothing for a rejected request
    # (per_success / free): tomba is per_success, so it is asked; when it rejects too, the caller gets
    # route_caller_fault naming both — and no paid-per-call provider was ever asked.
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(400, {"errors": [{"details": "bad"}]})], "tomba": [(400, {"error": "bad"})], "*": [(400, {"error": "bad"})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "route_caller_fault", r.text
    assert [p for p, *_ in seen][:2] == ["hunter", "tomba"] and len(seen) == 3, "at most two fallbacks, then the 4xx is the caller's"
    outcomes = {t["endpoint_id"]: t["outcome"] for t in r.json()["detail"]["tried"]}
    assert outcomes["hunter.people.email.find"] == "error" and outcomes["tomba.people.email.find"] == "error"
    # a scraper's "please retry" 400 (tikhub, live 2026-08-28) is why: the next free-on-failure provider answers
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(400, {"errors": [{"details": "bad"}]})],
         "tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "tomba.people.email.find", r.text


async def test_a_2xx_without_the_required_core_is_a_miss_not_a_hit(clients: AsyncClient, enrichment_on, monkeypatch):
    """A 200 whose body lacks the contract's required field (a null result under a success envelope)
    is a MISS: the waterfall goes on, and the verdict/hit-rate never counts it as answered."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": None, "score": None}})],
         "tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    await clients.post("/secrets", json={"name": "hunter", "value": "MY-HUNTER-KEY"})
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    outcomes = {t["endpoint_id"]: t["outcome"] for t in r.json()["_treg"]["tried"]}
    assert outcomes["hunter.people.email.find"] == "miss" and r.json()["_treg"]["served_by"] == "tomba.people.email.find"


async def test_catalog_get_on_the_routed_endpoint_shows_the_plan(clients: AsyncClient, enrichment_on):
    r = await clients.get(f"/catalog/endpoints/{ROUTED}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["endpoint"]["kind"] == "routed" and d["routing"]["contract"]["identity"]
    # the same job from unrouted providers is named here too — the search page points at this row
    also = {a["endpoint_id"] for a in d["routing"]["also"]}
    assert also.isdisjoint(d["endpoint"]["routed_children"]) and all(i.endswith("email.find") or "." in i for i in also)
    plan = d["routing"]["plan"]
    assert plan and plan[0]["usd"] <= plan[-1]["usd"] and plan[0]["accepts"]
    assert "hit_rate" not in plan[0], "unmeasured says nothing rather than nulls"
    assert {c["endpoint_id"] for c in plan} <= set(d["endpoint"]["routed_children"] if "routed_children" in d["endpoint"] else [c["endpoint_id"] for c in plan])


async def test_idempotent_replay_of_a_routed_call_never_calls_a_provider_twice(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    h = {"Idempotency-Key": "route-1"}
    r1 = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"}, headers=h)
    r2 = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200 and r2.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert r2.json() == r1.json() and len(seen) == 1


@pytest.mark.parametrize(
    ("terminal", "expected_status", "expected_error"),
    [
        ((400, {"message": "invalid email"}), 400, "route_caller_fault"),
        ((503, {"message": "provider down"}), 502, "route_failed"),
    ],
)
async def test_idempotent_replay_preserves_a_routed_failure_after_partial_charge(
    clients: AsyncClient, enrichment_on, monkeypatch, terminal, expected_status, expected_error,
):
    routed = "treg.people.email.verify"
    tomba_miss = (200, {"data": {"email": {"status": None, "score": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [tomba_miss, tomba_miss], "leadmagic": [terminal, terminal]},
        seen,
    ))
    headers = {
        "Idempotency-Key": "route-partially-charged-failure",
        "X-Treg-Route-Prefer": "tomba,leadmagic",
        "X-Treg-Route-Exclude": "hunter",
    }
    before = await _balance(clients)

    r1 = await clients.post(f"/call/{routed}", json={"email": "bad@example.com"}, headers=headers)
    r2 = await clients.post(f"/call/{routed}", json={"email": "bad@example.com"}, headers=headers)

    assert r1.status_code == expected_status and r1.json()["detail"]["error"] == expected_error
    assert r2.status_code == r1.status_code and r2.json() == r1.json()
    assert r2.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert r1.headers["X-Treg-Cost-Micro"] == r2.headers["X-Treg-Cost-Micro"] == "8900"
    assert before - await _balance(clients) == 8_900
    assert [provider for provider, *_ in seen] == ["tomba", "leadmagic"]


def test_a_per_success_miss_settles_at_zero_when_the_adapter_can_tell():
    """Live 2026-08-28: the first waterfall charged tomba, findymail and leadsforge for misses the
    catalog calls free. The adapter's `miss` predicate is the missing knowledge."""
    from test_marketplace_call import _mk
    from treg.application.call import settle as A
    miss_tomba = b'{"data": {"email": null, "score": null, "first_name": "Z", "verification": {"status": null}}}'
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.people.email.find", cost_type="per_success"), miss_tomba) == 0
    hit_tomba = b'{"data": {"email": "z@x.io", "score": 90}}'
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.people.email.find", cost_type="per_success"), hit_tomba) is None, "a hit still settles at the estimate"
    assert A._observed_cost_micro(_mk("findymail", endpoint_id="findymail.search.name", cost_type="per_success"), b'{"contact": {"email": null}}') == 0
    assert A._observed_cost_micro(_mk("leadsforge", endpoint_id="leadsforge.people.email.find", cost_type="per_success"), b'{"email": null, "status": "failed"}') == 0
    assert A._observed_cost_micro(_mk("leadsforge", endpoint_id="leadsforge.people.email.find", cost_type="per_call"), b'{"email": null}') is None, "per_call bills the call"
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.companies.emails.count", cost_type="per_success"), b'{"data": {}}') is None, "no adapter → no opinion"


async def test_discovery_puts_the_routed_parent_first_and_its_children_under_it(clients: AsyncClient):
    r = await clients.get("/catalog/search", params={"q": "find work email"})
    rows = r.json()["results"]
    ids = [x["id"] for x in rows]
    parent = ids.index(ROUTED)
    kids = [i for i, x in enumerate(rows) if x["capability"] == "people.email.find" and x["id"] != ROUTED]
    assert kids and parent < min(kids), "the routed parent leads its capability group"
    assert kids == list(range(parent + 1, parent + 1 + len(kids))), "children sit right under the parent"
    assert rows[parent]["routed_children"] and any("ROUTED" in h for h in r.json()["hints"])
    p = await clients.get("/catalog/platforms/people")
    group = next(c for c in p.json()["capabilities"] if c["id"] == "people.email.find")
    assert group["endpoints"][0]["id"] == ROUTED
    from treg.domain.catalog.store import group_routed
    plain = [{"id": "a", "capability": "x", "kind": "data"}, {"id": "b", "capability": "y", "kind": "data"}]
    assert group_routed(plain) == plain, "no routed row → order untouched"


async def test_hit_verdict_is_recorded_and_becomes_a_hit_rate(clients: AsyncClient, enrichment_on, monkeypatch):
    from treg.domain.catalog import stats
    from treg.models import CallRecord
    hit = (200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})
    miss = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [hit, miss, hit]}, seen))
    for _ in range(3):
        assert (await clients.get("/call/tomba.people.email.find?full_name=P%20C&domain=stripe.com")).status_code == 200
    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.endpoint_id == "tomba.people.email.find"))).scalars().all()
        assert sorted(r.hit for r in rows) == [False, True, True], "the verdict, never the body"
        # below the floor → None; the floor is about evidence, not a bug
        assert (await stats.observed(db, ["tomba.people.email.find"]))["tomba.people.email.find"]["hit_rate"] is None
        monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
        s = (await stats.observed(db, ["tomba.people.email.find"], per_success={"tomba.people.email.find"}))["tomba.people.email.find"]
        assert s["hit_rate"] == pytest.approx(2 / 3, abs=1e-3) and s["hit_samples"] == 3
        # historical rows without a verdict: a per-success 2xx with cost_observed 0 is a miss, > 0 a hit
        for r in rows:
            r.hit = None
            r.cost_observed_micro = 8_900 if r.status_code == 200 and "x" else 0
        rows[0].cost_observed_micro = 0
        await db.commit()
        s = (await stats.observed(db, ["tomba.people.email.find"], per_success={"tomba.people.email.find"}))["tomba.people.email.find"]
        assert s["hit_samples"] == 3 and s["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
        s = (await stats.observed(db, ["tomba.people.email.find"]))["tomba.people.email.find"]
        assert s["hit_samples"] == 0, "the zero-cost fallback applies to per-success endpoints only"
    # the plan reads it: with a measured hit rate the confidence flips from unmeasured to measured
    monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
    # the catalog reads observations through the process cache: a cold entry answers nothing and
    # refreshes in the background, so warm it the way test_endpoint_stats does
    from treg import api as A
    monkeypatch.setattr(call_route, "_endpoint_observation_reader", A.app.state.endpoint_observation_reader)
    await clients.get(f"/catalog/endpoints/{ROUTED}")
    await A.app.state.endpoint_observation_reader.wait_for_idle()
    r = await clients.get(f"/catalog/endpoints/{ROUTED}")
    tomba = next(c for c in r.json()["routing"]["plan"] if c["endpoint_id"] == "tomba.people.email.find")
    assert tomba["hit_rate"] == pytest.approx(2 / 3, abs=1e-3) and tomba["usd_per_hit"] == pytest.approx(0.0089, abs=1e-4)


async def test_a_registered_tool_for_a_provider_ranks_first_and_is_free(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    sid = (await clients.post("/secrets", json={"name": "my-hunter", "value": "OWN-HUNTER"})).json()["id"]
    r = await clients.post("/tools", json={"name": "our-hunter", "base_url": "https://api.hunter.io/v2", "secret_id": sid})
    assert r.status_code == 200, r.text
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "hunter.people.email.find" and r.json()["_treg"]["tier"] == "tool"
    assert await _balance(clients) == before and r.json()["_treg"]["charged_micro"] == 0


async def test_mcp_search_shows_the_routed_parent_first_with_its_children(clients: AsyncClient):
    from treg import mcp as M
    out = await M._catalog_search_impl("find work email", 12, surface=M._TEAM_SURFACE) if "surface" in M._catalog_search_impl.__code__.co_varnames else await M._catalog_search_impl("find work email", 12)
    ids = [r["endpoint_id"] for r in out["results"]]
    parent = ids.index(ROUTED)
    kids = [i for i, r in enumerate(out["results"]) if r["provider"] != "treg" and r["endpoint_id"].split(".", 1)[1] in ("people.email.find", "people.email.find.linkedin", "search.name")]
    assert kids and parent < min(kids)
    assert out["results"][parent]["routed"].startswith("treg picks among")


def test_filters_reach_adapters_through_in_expr_and_array_bodies():
    cat = catalog_store.load()
    contract = cat.contracts["google.keywords.ideas"]
    req, variant = canonical_identity(contract, {"keyword": "coffee"})
    assert variant == ("keyword",) and req["country"] == "us" and req["limit"] == 20, "filter defaults ride with the identity"
    req, _ = canonical_identity(contract, {"keyword": "coffee", "country": "GB", "limit": 5})
    q, b = cat.adapters["dataforseo.google.keywords.ideas"].to_upstream(req)
    assert b == [{"keyword": "coffee", "location_code": 2826, "language_code": "en", "limit": 5}], "task list body, GB → 2826"
    q, b = cat.adapters["seranking.google.keywords.ideas"].to_upstream(req)
    assert q == {"keyword": "coffee", "source": "uk", "limit": "5"}
    q, b = cat.adapters["serpapi.google.keywords.ideas"].to_upstream(req)
    assert q == {"q": "coffee", "gl": "gb", "hl": "en", "engine": "google_autocomplete"}
    q, b = cat.adapters["tomba.people.email.verify"].to_upstream({"email": "a@b.io"})
    assert q == {"email": "a@b.io"}, "a pathParams target travels as a query value the proxy folds into the path"
    assert cost_at({"usd": 0.00179, "type": "per_result", "per": 1}, req) == 8_950, "priced at the requested limit"
    ep = cat.by_id["treg.google.keywords.ideas"]
    assert ep["input"]["body"]["country"]["note"].startswith("filter — default 'us'")


async def test_a_keyless_provider_is_dropped_at_planning_not_failed_at_call_time(clients: AsyncClient, enrichment_on, monkeypatch):
    """Live 2026-08-28: exa is platform-eligible but this deployment held no exa key; the child's
    'no credential' 404 aborted the routed call. Planning must drop it and name why."""
    from treg.application.call.route import RouteOptions, build_plan
    cat = catalog_store.load()
    ep = cat.by_id["treg.people.email.find"]
    monkeypatch.setenv("TREG_PLATFORM_KEY_AVIATO", "")  # aviato stays eligible, but keyless
    get_settings.cache_clear()
    class _Org: id = 1
    class _Caller: org_id = 1; org = _Org()
    plan = await build_plan(ep, {"linkedin_url": "https://www.linkedin.com/in/x"}, _Caller(), RouteOptions.from_headers(lambda k: None))
    assert "aviato.people.email.find" not in [c.endpoint["id"] for c in plan.candidates]
    assert any(d["endpoint_id"] == "aviato.people.email.find" and "no aviato key" in d["why"] for d in plan.dropped)


def test_a_contract_may_set_its_own_default_ceiling():
    from treg.application.call.route import RouteOptions, DEFAULT_MAX_COST_MICRO
    cat = catalog_store.load()
    assert cat.contracts["people.search"].default_max_cost_usd is None, "the $1 default covers every current ladder"
    assert RouteOptions.from_headers(lambda k: None, 500_000).max_cost_micro == 500_000
    assert RouteOptions.from_headers(lambda k: None).max_cost_micro == DEFAULT_MAX_COST_MICRO
    assert RouteOptions.from_headers(lambda k: "0.02" if k == "x-treg-route-max-cost" else None, 500_000).max_cost_micro == 20_000


async def test_routed_call_and_access_name_the_providers_dropped_for_this_deployment(clients: AsyncClient, enrichment_on, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_AVIATO", "")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": None, "score": None, "verification": {"status": None}}})],
         "findymail": [(200, {"contact": {"name": "x", "email": None}})],
         "leadsforge": [(200, {"email": None, "status": "failed"})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"linkedin_url": "https://www.linkedin.com/in/x"}, headers={"X-Treg-Route-Max-Cost": "0.03"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    dropped = r.json()["_treg"]["dropped"]
    assert any(d["endpoint_id"] == "aviato.people.email.find" and "no aviato key" in d["why"] for d in dropped)
    hunter = next(d for d in dropped if d["endpoint_id"] == "hunter.people.email.find")
    assert hunter == {"endpoint_id": "hunter.people.email.find", "why": "needs {domain, full_name} | {domain, first_name, last_name}"}
    a = await clients.get(f"/catalog/endpoints/{ROUTED}/access")
    assert a.status_code == 200 and a.json()["tier"] == "routed" and a.json()["detail"].startswith("routed — ")
    assert "aviato.people.email.find" in a.json()["detail"]


def test_a_caller_may_send_everything_it_knows_and_each_provider_gets_only_its_variant():
    cat = catalog_store.load()
    contract = cat.contracts["people.phone.find"]
    everything = {"email": "p@stripe.com", "linkedin_url": "https://www.linkedin.com/in/p", "full_name": "Patrick Collison", "domain": "stripe.com"}
    ident, variant = canonical_identity(contract, everything)
    assert ident["first_name"] == "Patrick"
    from treg.domain.catalog.routing.contracts import adapter_accepts
    tomba = cat.adapters["tomba.people.phone.find"]
    v = adapter_accepts(tomba, ident)
    q, b = tomba.to_upstream(ident, v)
    assert q == {"email": "p@stripe.com"}, "tomba insists on exactly one identifier — only the matched variant is sent"
    lf = cat.adapters["leadsforge.people.phone.find"]
    q, b = lf.to_upstream(ident, adapter_accepts(lf, ident))
    assert b == {"firstName": "Patrick", "lastName": "Collison", "companyDomain": "stripe.com"}, "derived names, and not the LinkedIn URL"
    # filters always travel, whatever the variant
    kw = cat.contracts["google.keywords.ideas"]
    req, v = canonical_identity(kw, {"keyword": "coffee", "country": "de"})
    q, b = cat.adapters["seranking.google.keywords.ideas"].to_upstream(req, v)
    assert q == {"keyword": "coffee", "source": "de", "limit": "20"}
    # every adapter still verifies with the change
    assert all(a.verified for a in cat.adapters.values())


def test_rank_prefers_the_candidate_that_uses_more_of_the_identity():
    """Given {company_domain, title}, a title-aware provider outranks a cheaper domain-only one —
    the cheaper answer would be to a different question (the whole company)."""
    from treg.domain.catalog.routing.plan import Candidate, rank
    def cand(eid, variant, price):
        return Candidate(endpoint={"id": eid, "provider": eid.split(".")[0], "cost": {"type": "per_result"}}, adapter=None,
                         variant=variant, tier="platform", price_micro=price, hit_rate=None, ok_rate=None, p50_ms=None, last_ok_days=None)
    free_domain = cand("hunter.x.multi-domain-search", ("company_domain",), 0)
    title_aware = cand("icypeas.people.search", ("company_domain", "title"), 380)
    dearer_title = cand("companyenrich.people.search", ("company_domain", "title"), 19_600)
    given = {"company_domain", "title"}
    assert [c.endpoint["id"] for c in rank([free_domain, dearer_title, title_aware], given=given)] == [
        "icypeas.people.search", "companyenrich.people.search", "hunter.x.multi-domain-search"]
    # a key the caller did NOT send (reached via derive) earns nothing: price decides again
    assert rank([free_domain, title_aware], given={"company_domain"})[0] is free_domain
    # …but a variant DERIVED from what the caller sent covers it: {first,last,domain} from a supplied
    # full_name is as specific as {full_name, domain}, so the cheaper of the two (hunter) leads
    derive = {"first_name": "split_first(full_name)", "last_name": "split_last(full_name)"}
    hunter = cand("hunter.people.email.find", ("first_name", "last_name", "domain"), 4_900)
    apollo = cand("apollo.people.enrich", ("full_name", "domain"), 26_000)
    assert rank([apollo, hunter], given={"full_name", "domain"}, derive=derive)[0] is hunter


def test_a_provider_that_cannot_express_a_supplied_filter_ranks_last_among_equals():
    """Live 2026-08-29: `{q, title, location: London, country: GB}` went to the cheapest candidate,
    which had no place for either geo filter, and returned people in Bengaluru and San Francisco —
    reported as a hit. Cheapness must not buy an answer to a looser question."""
    from treg.domain.catalog.routing.plan import Candidate, ignored_filters, rank
    def cand(eid, price, ignored=()):
        return Candidate(endpoint={"id": eid, "provider": eid.split(".")[0], "cost": {"type": "per_result"}},
                         adapter=None, variant=("q",), tier="platform", price_micro=price, hit_rate=None,
                         ok_rate=None, p50_ms=None, last_ok_days=None, ignored=ignored)
    geo_blind = cand("aviato.people.search", 2_500, ignored=("country", "location"))
    geo_aware = cand("icypeas.people.search", 5_700)
    assert [c.endpoint["id"] for c in rank([geo_blind, geo_aware], given={"q"})] == [
        "icypeas.people.search", "aviato.people.search"], "the dearer provider that honours the filters leads"
    # still reachable when it is the only candidate, and price still decides among equals
    assert rank([geo_blind], given={"q"})[0] is geo_blind
    assert rank([geo_blind, cand("z.people.search", 9_000, ignored=("country", "location"))], given={"q"})[0] is geo_blind

    # and the set itself is read off the adapter's input map, not guessed
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    ident = {"q": "backend engineers", "country": "GB", "location": "London, United Kingdom", "limit": 15}
    assert "country" in ignored_filters(cat.adapters["aviato.people.search"], contract, ident)
    assert ignored_filters(cat.adapters["icypeas.people.search"], contract, ident) == (), \
        "icypeas is the only people.search adapter that maps geo — the rule must float it to the top"
    # the full_name variant has exactly two candidates and neither mapped `country` — so a GT search
    # went to New York and was billed (voice-ai-outbound, 2026-09-03). aviato's simple search takes
    # country NAMES (live 2026-09-04: `Guatemala` → 84,145 rows, `GT` → 0), hence country_name().
    simple = cat.adapters["aviato.people.search.simple"]
    by_name, _ = canonical_identity(contract, {"full_name": "Carlos Lopez", "country": "GT", "limit": 5})
    assert ignored_filters(simple, contract, by_name) == ()
    q, _ = simple.to_upstream(by_name, ("full_name",))
    assert q["country"] == "Guatemala", q  # a query value travels as one string, never a list repr
    assert "country" not in simple.to_upstream({**by_name, "country": None}, ("full_name",))[0]


async def test_the_geo_aware_child_wins_a_filtered_search_and_the_answer_says_what_was_dropped(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """End to end on the real people.search ladder: the caller sends geo, so the child that maps it
    is called even though a CHEAPER one is callable — and when the winner drops a filter, the
    envelope and a header say so, where a caller will see it (not buried in `tried[]`).

    Live 2026-08-29 this went to aviato ($0.0025, maps neither `country` nor `location`) and came
    back with people in Bengaluru and San Francisco for a London brief, reported as a hit."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,icypeas")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, {"leads": [{"firstname": "Aleksei", "lastname": "Strizhak", "lastJobTitle": "Senior Backend Engineer",
                                       "address": "London Area, United Kingdom", "profileUrl": "https://linkedin.com/in/as"}]})],
         "*": [(200, {"persons": []})] * 12}, seen))
    r = await clients.post("/call/treg.people.search",
                           json={"q": "backend engineer", "title": "Backend Engineer",
                                 "location": "London, United Kingdom", "country": "GB", "limit": 15})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "icypeas.people.search", \
        "the child that maps country/location leads, though the cheaper geo-blind aviato is callable"
    assert seen == [("icypeas", "POST", {}, {"query": {"currentJobTitle": {"include": ["Backend Engineer"]},
                                                      "location": {"include": ["London, United Kingdom"]}},
                                             "pagination": {"size": 15}})], \
        "one call, and the geography actually reached the provider"
    assert "ignored_filters" not in d["_treg"] and "X-Treg-Ignored-Filters" not in r.headers

    # …and when the winner cannot express a filter, the answer says which — envelope, header, attempt
    seen2 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"*": [(200, {"items": [{"fullName": "Ada L", "URLs": {"linkedin": "linkedin.com/in/al"}}], "count": {"value": 1}})] * 12}, seen2))
    r2 = await clients.post("/call/treg.people.search", json={"q": "backend engineer", "country": "GB", "limit": 15})
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["_treg"]["served_by"] == "aviato.people.search", "no geo-aware child answers a {q}-only brief here"
    assert d2["_treg"]["ignored_filters"] == ["country"], "the caller sent it; aviato has no place for it"
    assert r2.headers["X-Treg-Ignored-Filters"] == "country"
    assert d2["_treg"]["tried"][-1]["ignored_filters"] == ["country"], "same set in all three places"


async def test_routed_discovery_is_a_runtime_switch(clients: AsyncClient, enrichment_on, monkeypatch):
    """`TREG_ROUTED_DISCOVERY=off` stops search LEADING with `treg.<capability>` — nothing else.
    The endpoints stay callable, priced and reachable by id; only the steering goes away, so a
    deployment can answer "should every agent be pointed at the router by default" with traffic
    instead of an argument, and can undo it without a redeploy."""
    q = {"q": "find someone's work email from their name and company", "limit": 8}
    on = (await clients.get("/catalog/search", params=q)).json()
    ids_on = [r["id"] for r in on["results"]]
    assert any(i.startswith("treg.") for i in ids_on), "steering on: the routed row is in the page"

    monkeypatch.setenv("TREG_ROUTED_DISCOVERY", "off")
    get_settings.cache_clear()
    off = (await clients.get("/catalog/search", params=q)).json()
    ids_off = [r["id"] for r in off["results"]]
    assert not any(i.startswith("treg.") for i in ids_off), \
        "steering off: search looks as it did before routing shipped"
    assert ids_off, "and it still returns the providers themselves"

    # every OTHER discovery surface follows the same switch, or the deployment contradicts itself
    plat = (await clients.get("/catalog/platforms/people")).json()
    flat = json.dumps(plat)
    assert "treg.people." not in flat, "browse view: no routed row while steering is off"
    for path in ("/skill.md", "/llms.txt"):
        body = (await clients.get(path)).text
        assert "Routed endpoints" not in body, f"{path} must not teach what search hides"
        assert "<!--routed" not in body, f"{path} leaked a marker"
        assert "provider_capacity_unavailable" in body, f"{path} lost the unrelated overflow guidance"

    # …but the endpoint is untouched: still callable, still priced, still found by id
    r = await clients.get("/catalog/endpoints/treg.people.email.find")
    assert r.status_code == 200 and r.json()["endpoint"]["kind"] == "routed"
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "p@stripe.com", "score": 99, "first_name": "Patrick",
                                   "last_name": "Collison", "verification": {"status": "valid"}}})]}, []))
    call = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert call.status_code == 200 and call.json()["_treg"]["served_by"] == "tomba.people.email.find"
    get_settings.cache_clear()

def test_keywords_are_a_filter_so_they_reach_every_provider_that_can_express_them():
    """The brief's SUBSTANCE lives in its keywords. As identity they would be dropped whenever
    another variant matched — icypeas matches {title}, so a `q` carrying "microservices" never
    reached it and the search degenerated to title+location (bench 2026-08-29: "football scouting
    analysts" reached the provider as title="Football Analyst" and scored 0 qualified of 15)."""
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    assert "keywords" in contract.filters and "keywords" not in {k for v in contract.identity for k in v}
    ident, variant = canonical_identity(contract, {
        "q": "backend developers with microservices", "title": "Backend Engineer",
        "location": "London, United Kingdom", "country": "GB",
        "keywords": ["microservices", "architecture"], "limit": 15})
    from treg.domain.catalog.routing.contracts import adapter_accepts
    icy = cat.adapters["icypeas.people.search"]
    _, body = icy.to_upstream(ident, adapter_accepts(icy, ident))
    assert body["query"]["keyword"]["include"] == ["microservices", "architecture"], \
        "icypeas takes them natively — the whole point of the contract field"
    exa = cat.adapters["exa.people.search"]
    _, body = exa.to_upstream(ident, adapter_accepts(exa, ident))
    assert "microservices" in body["query"], "a semantic provider gets them folded into the query"
    # and a provider with nowhere to put them says so, which ranks it down (PR #254)
    from treg.domain.catalog.routing.plan import ignored_filters
    assert "keywords" in ignored_filters(cat.adapters["aviato.people.search"], contract, ident)


async def test_a_thin_hit_does_not_end_the_waterfall_when_the_caller_set_min_results(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """`X-Treg-Route-Min-Results: 3` — one row is not an answer to "find me candidates". The
    router keeps going and the FULLEST answer wins; without it the first non-empty body stops the
    search (bench 2026-08-29: the hand-written policy's `if len(rows) < 3 -> fall through` was the
    single behaviour the routed path could not express)."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,icypeas")
    get_settings.cache_clear()
    thin = {"leads": [{"firstname": "Solo", "lastname": "Row", "profileUrl": "https://linkedin.com/in/s"}]}
    full = {"items": [{"fullName": f"P{i}", "URLs": {"linkedin": f"linkedin.com/in/p{i}"}} for i in range(9)],
            "count": {"value": 9}}
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, thin)], "*": [(200, full)] * 12}, seen))
    body = {"q": "backend engineer", "title": "Backend Engineer",
            "location": "London, United Kingdom", "country": "GB", "limit": 15}
    r = await clients.post("/call/treg.people.search", json=body, headers={"X-Treg-Route-Min-Results": "3"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["outcome"] for t in d["_treg"]["tried"]][0] == "weak", "1 row < 3 is not an answer"
    assert d["_treg"]["served_by"] != "icypeas.people.search" and len(d["output"]["people"]) == 9

    # default (min_results 1): the same thin answer ends the search, as before
    seen2 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, thin)], "*": [(200, full)] * 12}, seen2))
    r2 = await clients.post("/call/treg.people.search", json=body)
    assert r2.json()["_treg"]["served_by"] == "icypeas.people.search" and len(seen2) == 1

    # and when NOBODY clears the bar, the fullest weak answer is still returned, not a miss
    seen3 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, thin)], "*": [(200, {"items": [{"fullName": "Two"}, {"fullName": "Rows"}],
                                                "count": {"value": 2}})] * 12}, seen3))
    r3 = await clients.post("/call/treg.people.search", json=body, headers={"X-Treg-Route-Min-Results": "5"})
    assert r3.status_code == 200 and len(r3.json()["output"]["people"]) == 2, "best effort beats nothing"


async def test_the_weak_hit_fallback_is_bounded_like_the_error_fallback(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """Some briefs HAVE only one right answer ("who runs engineering at X"), so no provider ever
    clears min_results and an unbounded rule pays the whole ladder on every call. Measured on the
    bench's deterministic set: 12.7x ($1.76 -> $22.35 over 28 queries) for answers already correct.
    At most MAX_WEAK_FALLBACKS extra providers are asked, then the fullest answer is returned."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,icypeas")
    get_settings.cache_clear()
    # Every provider answers ONE row in ITS OWN shape — a real thin HIT, not a miss (a miss does
    # not consume the bound, and must not: the bound is about paying for thin answers).
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas":    [(200, {"leads": [{"firstname": "One", "lastname": "Row"}], "total": 1})] * 4,
         "leadsforge": [(200, {"leads": [{"firstName": "One", "lastName": "Row"}]})] * 4,
         "leadmagic":  [(200, {"data": [{"full_name": "One Row"}], "total_count": 1})] * 4,
         "hunter":     [(200, {"data": {"emails": [{"value": "one@stripe.com"}]}, "meta": {"results": 1}})] * 4,
         "*":          [(200, {"items": [{"fullName": "One Row"}], "count": {"value": 1}})] * 8}, seen))
    # {company_domain} has the deepest ladder, so the BOUND is what stops this, not running out
    body = {"company_domain": "stripe.com", "limit": 15}
    r = await clients.post("/call/treg.people.search", json=body,
                           headers={"X-Treg-Route-Min-Results": "5"})   # nothing will ever clear 5
    assert r.status_code == 200, r.text
    d = r.json()
    attempts = [t for t in d["_treg"]["tried"] if t["outcome"] == "weak"]
    assert len(attempts) == call_route.MAX_WEAK_FALLBACKS + 1, \
        f"the first ask plus at most {call_route.MAX_WEAK_FALLBACKS} more, not the whole ladder"
    assert len(seen) == len(attempts), "and no provider beyond the bound was ever called"
    assert len(d["output"]["people"]) == 1, "and the caller still gets the answer that exists"


async def test_merge_unions_the_rows_the_caller_already_paid_for(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """`X-Treg-Route-Merge: 1` — a list answer is the one shape a union makes sense for, and the
    caller is charged for EVERY attempt already (`charged_micro` sums them), so returning only the
    winner's rows throws away results the team bought. Bench 2026-08-29: a people.search that fell
    through returned the fullest single provider's rows, never icypeas' 5 plus exa's 10."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,icypeas")
    get_settings.cache_clear()
    icy = {"leads": [{"firstname": "Ada", "lastname": "L", "profileUrl": "https://linkedin.com/in/ada"},
                     {"firstname": "Bo", "lastname": "M", "profileUrl": "https://linkedin.com/in/bo"}], "total": 2}
    # one row OVERLAPS on the profile url (different casing/scheme), one is new
    other = {"items": [{"fullName": "Ada L", "URLs": {"linkedin": "www.linkedin.com/in/Ada/"}},
                       {"fullName": "Cy N", "URLs": {"linkedin": "linkedin.com/in/cy"}}], "count": {"value": 2}}
    body = {"q": "backend engineer", "title": "Backend Engineer", "limit": 15}

    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, icy)], "*": [(200, other)] * 8}, seen))
    r = await clients.post("/call/treg.people.search", json=body,
                           headers={"X-Treg-Route-Min-Results": "5", "X-Treg-Route-Merge": "1"})
    assert r.status_code == 200, r.text
    d = r.json()
    people = d["output"]["people"]
    keys = sorted(call_route._row_key(p) for p in people)
    assert keys == ["linkedin.com/in/ada", "linkedin.com/in/bo", "linkedin.com/in/cy"], \
        "the union of both providers, and Ada — who both returned, spelled differently — appears ONCE"
    assert len(people) == 3, "3 distinct people from two answers of 2 rows each"
    assert len(d["_treg"]["merged_from"]) >= 2 and "X-Treg-Merged-From" in r.headers
    assert d["_treg"]["charged_micro"] == int(r.headers["X-Treg-Cost-Micro"]), \
        "merging changes no money: the sum over attempts is what it always was"

    # without the header the winner's rows alone come back — the pre-existing contract
    seen2 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, icy)], "*": [(200, other)] * 8}, seen2))
    r2 = await clients.post("/call/treg.people.search", json=body,
                            headers={"X-Treg-Route-Min-Results": "5"})
    assert "merged_from" not in r2.json()["_treg"] and len(r2.json()["output"]["people"]) == 2


def test_a_per_success_endpoint_with_no_adapter_settles_on_the_providers_own_success_rule():
    """The adapter's `miss` predicate covers routed children; 1330 of 1517 per_success endpoints
    have no adapter, and they are the SCRAPERS — whose failure mode is an HTTP 200 carrying an
    error code. Those providers publish a success rule and the catalog records it as `expect`,
    which until now only `scripts/catalog_verify.py` read.

    Live 2026-08-29: `justoneapi.x.linkedin-search-user-v1` answered
    `{"code": 301, "message": "COLLECT FAILED, SEND REQUEST AGAIN"}` — free on the vendor's own
    published terms ("only a code-0 response is billed") — and treg settled $0.0295 against the
    caller."""
    from test_marketplace_call import _mk
    from treg.application.call import settle as A
    cat = catalog_store.load()
    eid = "justoneapi.x.linkedin-search-user-v1"
    ep = cat.by_id[eid]
    assert (ep.get("cost") or {}).get("type") == "per_success" and cat.adapters.get(eid) is None, \
        "the shape this rule exists for: priced per success, no adapter to ask"
    assert ep.get("expect") == {"json_path": "code", "equals": 0}, "the loader must carry the rule"

    mk = _mk("justoneapi", endpoint_id=eid, cost_type="per_success")
    fail = b'{"code": 301, "data": null, "message": "COLLECT FAILED, SEND REQUEST AGAIN"}'
    assert A._observed_cost_micro(mk, fail) == 0, "a vendor-side failure the vendor does not bill"
    ok = b'{"code": 0, "data": {"users": [{"name": "Ada"}]}}'
    assert A._observed_cost_micro(mk, ok) is None, "a real hit still settles at the estimate"

    # a nested rule form (dataforseo's task envelope) reads the same way — picking one that also
    # has no adapter, since an adapter's own predicate takes precedence when there is one
    dfs = [e for e in cat.by_id.values()
           if (e.get("expect") or {}).get("json_path") == "tasks.0.status_code"
           and cat.adapters.get(e["id"]) is None
           and (e.get("cost") or {}).get("type") == "per_success"]
    if dfs:
        m2 = _mk(dfs[0]["provider"], endpoint_id=dfs[0]["id"], cost_type="per_success")
        assert A._observed_cost_micro(m2, b'{"tasks": [{"status_code": 40501}]}') == 0
        assert A._observed_cost_micro(m2, b'{"tasks": [{"status_code": 20000}]}') is None

async def test_a_declared_miss_status_is_a_miss_not_a_caller_fault(clients: AsyncClient, enrichment_on, monkeypatch):
    """aviato answers HTTP 404 `Not Found` for a person it has no record of. The endpoint's YAML says
    so (`miss: {status: 404}`), and the router must read it: before this a waterfall in which the
    other providers all missed ended in a 502 `route_failed` (live 2026-09-03, voice-ai-outbound —
    768 of 1,824 phone.find 502s in 30 days had no failure but an aviato 404), when the honest
    answer was a 200 miss."""
    routed = "treg.people.phone.find"
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"aviato": [(404, {"message": "Not Found"})],
         "tomba": [(200, {"data": {"e164_format": None}})],
         "leadmagic": [(200, {"mobile_number": None, "credits_consumed": 0})],
         "findymail": [(200, {"phone": None})],
         "leadsforge": [(200, {"phoneNumber": None})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{routed}", json={"linkedin_url": "https://www.linkedin.com/in/nobody-here"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Treg-Route-Outcome"] == "miss" and r.json()["_treg"]["served_by"] is None
    outcomes = {t["endpoint_id"]: t["outcome"] for t in r.json()["_treg"]["tried"]}
    assert outcomes["aviato.people.phone.find"] == "miss" and len(seen) == 5, "the 404 is a miss; every provider was still asked"
    assert await _balance(clients) == before, "nobody found anything, nothing was charged"
    # an UNDECLARED 4xx keeps its meaning: a vendor rejecting the request is still the caller's fault
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"aviato": [(422, {"message": "bad identifier"})], "*": [(422, {"message": "bad"})] * 4}, seen))
    r = await clients.post(f"/call/{routed}", json={"linkedin_url": "https://www.linkedin.com/in/nobody-here"},
                           headers={"X-Treg-Route-Prefer": "aviato"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_caller_fault", r.text
    # and with the waterfall off, the declared 404 alone is the (free) miss the caller asked for
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"aviato": [(404, {"message": "Not Found"})]}, seen))
    r = await clients.post(f"/call/{routed}", json={"linkedin_url": "https://www.linkedin.com/in/nobody-here"},
                           headers={"X-Treg-Route-Prefer": "aviato", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.headers["X-Treg-Route-Outcome"] == "miss" and len(seen) == 1, r.text
    assert r.json()["output"]["phone"] is None


def test_every_declared_miss_status_names_its_meaning():
    """`miss: {status, means}` is agent-facing (`endpoint_view`) and router-facing: both halves
    are required. The router honours a 4xx only — a `status: 200` block is documentation for the
    agent (tikhub answers 200 with a null body for an unknown id) and the adapter's own predicate
    decides that case, so `_miss_status` must never turn a success into a miss."""
    cat = catalog_store.load()
    declared = {e["id"]: e["miss"] for e in cat.by_id.values() if e.get("miss")}
    assert "aviato.people.phone.find" in declared and "hunter.people.enrich" in declared
    for eid, m in declared.items():
        assert isinstance(m, dict) and m.get("status") is not None and m.get("means"), eid
        assert int(m["status"]) < 500, f"{eid}: a 5xx is never 'asked and answered'"
    assert call_route._miss_status(cat.by_id["aviato.people.phone.find"]) == 404
    assert call_route._miss_status(cat.by_id["tikhub.x.reddit-app-fetch-post-comments"]) is None
    assert call_route._miss_status({"id": "x"}) is None


async def test_lusha_is_the_last_rung_of_the_phone_waterfall_and_settles_on_its_own_bill(clients: AsyncClient, enrichment_on, monkeypatch):
    """Guatemala, 2026-09-03: 7 phones in 44 across tomba/aviato/leadmagic/findymail/leadsforge.
    Lusha's native direct-dial data is the sixth rung — dearest per hit (6 credits), so it ranks
    last and is only asked once the cheap five have missed; a miss is free and a matched profile
    with no number costs the 1-credit search, both read off `billing.creditsCharged`."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_LUSHA", "PLATFORM-LUSHA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,lusha")
    get_settings.cache_clear()
    routed = "treg.people.phone.find"
    plan = (await clients.get(f"/catalog/endpoints/{routed}")).json()["routing"]["plan"]
    assert plan[-1]["endpoint_id"] == "lusha.people.phone.find" and len(plan) == 6, [c["endpoint_id"] for c in plan]
    def misses():
        return {"aviato": [(404, {"message": "Not Found"})], "tomba": [(200, {"data": {"e164_format": None}})],
                "leadmagic": [(200, {"mobile_number": None, "credits_consumed": 0})],
                "findymail": [(200, {"phone": None})], "leadsforge": [(200, {"phoneNumber": None})]}
    seen = []
    hit = {"requestId": "r", "results": [{"id": "v1.x", "fullName": "Ana Perez",
                                          "phones": [{"number": "+502 5555 0100", "type": "mobile", "doNotCall": False, "countryIso2": "GT"}]}],
           "billing": {"creditsCharged": 6, "resultsReturned": 1}}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({**misses(), "lusha": [(200, hit)]}, seen))
    # a Lusha attempt RESERVES the 6-credit hit price (~$0.75): on the $1.00 signup grant a team gets
    # one attempt, so fund the second call here rather than let the reserve mask the miss rule
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.grant(db, org_id, amount_micro=5_000_000, kind="test-funding", once=False)
        await db.commit()
    before = await _balance(clients)
    r = await clients.post(f"/call/{routed}", json={"full_name": "Ana Perez", "domain": "acme.gt"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "lusha.people.phone.find", r.text
    assert r.json()["output"] == {"phone": "+502 5555 0100", "line_type": "mobile", "country_code": "GT"}
    # {full_name, domain} is accepted by two rungs only (leadsforge, lusha); the four that need a
    # LinkedIn URL or an email are not candidates for this identity at all
    assert [p for p, *_ in seen] == ["leadsforge", "lusha"], "asked last, after every cheaper candidate missed"
    body = seen[-1][3]
    assert body == {"contacts": [{"firstName": "Ana", "lastName": "Perez", "companyDomain": "acme.gt"}], "reveal": ["phones"]}
    rate = catalog_store.load().credit_rates["lusha"]
    assert before - await _balance(clients) == int(6 * rate * 1_000_000 + 0.5), "the bill is Lusha's own creditsCharged"
    # a matched profile with no number is a MISS that still cost the 1-credit search
    seen.clear()
    no_number = {"requestId": "r", "results": [{"id": "v1.x", "fullName": "Ana Perez", "partialProfile": False}],
                 "billing": {"creditsCharged": 1, "resultsReturned": 1}, "status": "partial", "statusReason": "WATERFALL_NOT_CONFIGURED"}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({**misses(), "lusha": [(200, no_number)]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{routed}", json={"full_name": "Ana Perez", "domain": "acme.gt"})
    assert r.status_code == 200 and r.headers["X-Treg-Route-Outcome"] == "miss" and r.json()["output"]["phone"] is None, r.text
    assert before - await _balance(clients) == int(1 * rate * 1_000_000 + 0.5)
    get_settings.cache_clear()


async def test_strict_filters_refuses_a_looser_answer_instead_of_billing_it(clients: AsyncClient, enrichment_on, monkeypatch):
    """voice-ai-outbound, 2026-09-03: `{full_name, country: GT}` went to a candidate that ignored
    the country and was billed for people in New York. Opt-in, the caller is refused instead —
    unbilled, told which filter, and what identity a filter-aware provider would take."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRUSTDATA", "PLATFORM-CRUSTDATA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,crustdata")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, {"profiles": [{"name": "Someone"}], "total_count": 1})] * 3}, seen))
    before = await _balance(clients)
    # crustdata's people.search takes full_name and nothing geographic: with the header it is dropped
    r = await clients.post("/call/treg.people.search", json={"full_name": "Carlos Lopez", "country": "GT", "limit": 3},
                           headers={"X-Treg-Route-Strict-Filters": "1", "X-Treg-Route-Exclude": "aviato"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "no_route_candidate", r.text
    d = r.json()["detail"]
    assert seen == [] and await _balance(clients) == before, "refused before any provider was asked; nothing billed"
    assert any(x["endpoint_id"] == "crustdata.people.search" and x.get("strict") and "country" in x["why"] for x in d["dropped"]), d
    assert "X-Treg-Route-Strict-Filters" in d["message"] and "full_name" in d["message"]
    # without the header the same call goes out, is billed, and says what it ignored
    r = await clients.post("/call/treg.people.search", json={"full_name": "Carlos Lopez", "country": "GT", "limit": 3},
                           headers={"X-Treg-Route-Exclude": "aviato"})
    assert r.status_code == 200 and r.headers["X-Treg-Ignored-Filters"] == "country" and r.json()["_treg"]["ignored_filters"] == ["country"], r.text
    assert len(seen) == 1
    # and a candidate that CAN express the filter is unaffected by the header (aviato's simple search maps country)
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"aviato": [(200, {"items": [{"fullName": "Carlos Lopez", "location": "Guatemala"}], "totalResults": 1})]}, seen))
    r = await clients.post("/call/treg.people.search", json={"full_name": "Carlos Lopez", "country": "GT", "limit": 3},
                           headers={"X-Treg-Route-Strict-Filters": "1"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "aviato.people.search.simple", r.text
    assert "X-Treg-Ignored-Filters" not in r.headers and seen[0][2]["country"] == "Guatemala"
    get_settings.cache_clear()
