"""The shadow provider run: synthetic calls and a mocked candidate; no production data or network."""
import json

import httpx
from sqlalchemy import select

from treg.application import shadow as service
from treg.config import get_settings
from treg.domain.catalog import shadow as rules, store
from treg.domain.catalog.routing import paths as P
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot, CallRecord, ShadowPair

EP = "hunter.companies.enrich"
SPEC = {
    "base_url": "https://candidate.example",
    "units_header": "x-units",
    "capabilities": {"companies.enrich": {
        "method": "GET", "path": "/company", "accepts": [["domain"]], "in": {"domain": "queryParams.domain"},
        "out": {"name": "data.name", "domain": "data.domain", "industry": "data.industry",
                "with_email": "count_with(data.people, 'email')"},
        "miss": "data == null"}},
}


def test_spec_reads_back_a_request_and_compares_two_answers():
    sp = rules.parse(json.dumps(SPEC))
    target = sp.targets["companies.enrich"]
    identity = rules.request_identity(store.load().adapters[EP], {"domain": "acme.example"}, {})
    assert identity == {"domain": "acme.example"}
    assert rules.shadow_request(target, identity) == ({"domain": "acme.example"}, {})
    assert rules.shadow_request(target, {"name": "Acme"}) is None
    agreed = rules.compare({"name": "Acme GmbH", "domain": "https://www.acme.example/about", "industry": "Software"},
                           {"name": "ACME", "domain": "acme.example", "industry": "Dentists"})
    assert agreed == {"domain": True, "industry": False, "name": True}
    assert rules.compare({"name": "Acme"}, {"name": None}) == {}


def test_sampling_is_deterministic_and_off_spec_is_none():
    sp = rules.parse(json.dumps({**SPEC, "sample": {"companies.enrich": 0.0}}))
    assert not rules.sampled("companies.enrich", 7, sp, "s")
    assert rules.sampled("companies.enrich", 7, rules.parse(json.dumps(SPEC)), "s")
    assert rules.parse("") is None
    assert P.evaluate("count_with(rows, 'email')", {"rows": [{"email": "a"}, {"email": ""}, {}]}) == 1


async def _served_call(domain: str, response: dict, *, status=200) -> int:
    async with session_maker() as db:
        row = CallRecord(user_email="synthetic@example.test", tool_name=EP, method="GET", path="/v2/companies/find",
                         status_code=status, endpoint_id=EP, provider="hunter", credential_tier="platform",
                         cost_charged_micro=24000)
        db.add(row)
        await db.flush()
        key = ArchiveKey(key_hash=f"synthetic-{row.id}", endpoint_id=EP,
                         req_url=f"https://api.hunter.io/v2/companies/find?domain={domain}")
        db.add(key)
        await db.flush()
        db.add(ArchiveSnapshot(key_id=key.id, content_hash=f"body-{row.id}", body=json.dumps(response).encode()))
        row.archive_key_hash, row.archive_content_hash = key.key_hash, f"body-{row.id}"
        await db.commit()
        return row.id


async def test_collect_pairs_served_calls_with_the_candidate_and_keeps_no_values(clients, monkeypatch):
    monkeypatch.setenv("TREG_SHADOW_PROVIDER", json.dumps(SPEC))
    monkeypatch.setenv("TREG_SHADOW_PROVIDER_KEY", "candidate-key")
    monkeypatch.setenv("TREG_SHADOW_PER_SECOND", "1000")
    get_settings.cache_clear()
    assert (await service.collect(session_factory=session_maker))["status"] == "started"

    await _served_call("acme.example", {"data": {"name": "Acme Inc", "domain": "acme.example",
                                                  "category": {"industry": "Software"}}})
    await _served_call("ghost.example", {"data": {"name": None}})
    await _served_call("busy.example", {"data": {"name": "Busy Ltd", "domain": "busy.example"}})
    seen = []

    def candidate(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.url.params.get("domain"), request.headers.get("authorization")))
        if request.url.params.get("domain") == "acme.example":
            return httpx.Response(200, headers={"x-units": "1"}, json={"data": {
                "name": "ACME", "domain": "acme.example", "industry": "Software",
                "people": [{"email": "a@acme.example"}, {"email": None}]}})
        if request.url.params.get("domain") == "busy.example":
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(404, json={"error": "no such company"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(candidate))
    result = await service.collect(session_factory=session_maker, client=client, max_seconds=10)
    assert result["paired"] == 3
    assert sorted(seen) == [("/company", "acme.example", "Bearer candidate-key"),
                            ("/company", "busy.example", "Bearer candidate-key"),
                            ("/company", "ghost.example", "Bearer candidate-key")]
    async with session_maker() as db:
        pairs = (await db.execute(select(ShadowPair).order_by(ShadowPair.id))).scalars().all()
    hit, miss, _refused = pairs
    assert (hit.served_found, hit.shadow_found, hit.shadow_with_email, hit.shadow_units) == (True, True, 1, 1.0)
    assert hit.fields == {"domain": True, "industry": True, "name": True}
    assert (miss.served_found, miss.shadow_found, miss.shadow_status) == (False, False, 404)
    assert "acme" not in json.dumps([p.model_dump(mode="json") for p in pairs])

    again = await service.collect(session_factory=session_maker, client=client, max_seconds=10)
    assert again["paired"] == 0
    report = await service.report(session_factory=session_maker)
    row = report["rows"][0]
    # The 429 is no answer: it counts as an error and stays out of the hit rates.
    assert (row["pairs"], row["answered"], row["shadow_error_pct"]) == (3, 2, 33.3)
    assert (row["served_hit_pct"], row["shadow_hit_pct"], row["shadow_hit_with_email_pct"]) == (50.0, 50.0, 50.0)
    assert row["agreement_pct"] == {"domain": 100.0, "industry": 100.0, "name": 100.0}
    await client.aclose()


async def test_collect_is_off_without_the_setting(clients, monkeypatch):
    monkeypatch.delenv("TREG_SHADOW_PROVIDER", raising=False)
    get_settings.cache_clear()
    assert await service.collect(session_factory=session_maker) == {"status": "off"}


def test_role_words_become_seniority_and_department_and_one_item_lists_unwrap():
    assert P.evaluate("role_seniority(t)", {"t": ["CMO", "VP Marketing", "Head of Growth"]}) == ["c_level", "vp", "director"]
    assert P.evaluate("role_department(t)", {"t": ["CMO", "VP Marketing", "Head of Growth"]}) == ["marketing"]
    assert P.evaluate("role_seniority(t)", {"t": "Software Engineer"}) is None
    assert P.evaluate("scalar(d)", {"d": ["acme.example"]}) == "acme.example"
    identity = rules.request_identity(store.load().adapters["quickenrich.people.search"], {},
                                      {"company_url": {"include": ["acme.example"]}, "title": {"include": ["CMO"]}})
    assert identity == {"company_domain": "acme.example", "title": "CMO"}
