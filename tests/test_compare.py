"""Pairwise comparisons: the hourly worker run, the invitation, the blinded API and the team MCP
tool (application/compare.py, docs feedback.md "Pairwise comparisons"). Synthetic calls and a
stubbed relay: no production data, no network."""
from __future__ import annotations

from urllib.parse import urlsplit
import json
import uuid
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import pytest
from sqlmodel import select

from treg import hints
from treg.application import compare
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import Settings, get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing.contracts import adapter_accepts, canonical_identity
from treg.infra import kv
from treg.infra.db import session_maker
from treg.models import (ArchiveKey, ArchiveSnapshot, CallComparison, CallRecord, CallReview, CapabilityPin,
                         DenyRule, Membership, Org)
from treg.timeutil import utcnow_naive

PAIR = {"web.search": [["tavily", "exa"]]}
TAVILY_HIT = {"results": [{"title": "Tavily says", "url": "https://example.com/t"}], "usage": {"credits": 1}}
EXA_HIT = {"results": [{"title": "Exa says", "url": "https://example.com/e"}], "costDollars": {"total": 0.007}}


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TAVILY", "PLATFORM-TAVILY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "PLATFORM-EXA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tavily,exa")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _compare_on(monkeypatch, *, pairs=PAIR, **values):
    settings = get_settings()
    for field, value in {"compare_rate": 1.0, "compare_pairs": json.dumps(pairs),
                         "compare_max_per_run": 20, "compare_max_per_day": 50,
                         "compare_lookback_hours": 2, "compare_budget_per_hour": 3,
                         "compare_team_cap": 10, **values}.items():
        monkeypatch.setattr(settings, field, value)
    monkeypatch.setattr(compare, "CALL_GAP_S", 0)


def _relay_b(monkeypatch, *, status=200, body=None, before=None):
    """The relay for B: each call recorded; the call path's own relay must never run."""
    sent = []

    async def relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        payload = b""
        async for chunk in request.body_stream():
            payload += chunk
        sent.append({"url": upstream_url, "tool": tool, "query": dict(request.query_items),
                     "body": json.loads(payload) if payload else None})
        if before is not None:
            await before()
        if status == 0:
            raise httpx.ConnectError("unreachable")
        provider = "exa" if "exa" in upstream_url else "tavily"
        raw = json.dumps(body if body is not None else {"tavily": TAVILY_HIT, "exa": EXA_HIT}[provider]).encode()

        async def stream():
            yield raw

        async def close():
            return None
        return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)

    async def never(*a, **kw):
        raise AssertionError("the call path's relay ran")
    monkeypatch.setattr(compare, "relay", relay)
    monkeypatch.setattr(call_service, "relay", never)
    return sent


async def _org_id(clients) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _routed_call(org_id: int, q: str, *, a: str = "tavily", answer: dict | None = None,
                       tier: str = "platform", minutes_ago: float = 5, archived: bool = True,
                       capability: str = "web.search", given: dict | None = None,
                       marked: bool = True, cached: bool = False, reviewing: bool = True) -> str:
    """A routed parent and its served child, with the child's request and answer in the archive,
    and the call path's eligibility marker, as a real sampled routed call leaves them. `reviewing`
    gives the team one recent review, which a team needs to be compared at all."""
    if reviewing:
        async with session_maker() as db:
            if not (await db.execute(
                    select(CallReview).where(CallReview.org_id == org_id))).first():
                db.add(CallReview(org_id=org_id, user_email="tim@superdesign.dev",
                                  call_id=f"reviewed-{org_id}", endpoint_id="tavily.web.search",
                                  usefulness="useful", invited=True, client="mcp"))
                await db.commit()
    cat = catalog_store.load()
    eid = next(e["id"] for e in cat.for_capability(capability)
               if e["provider"] == a and e["id"] in cat.adapters)
    adapter = cat.adapters[eid]
    identity, _ = canonical_identity(cat.contracts[capability], given or {"q": q})
    query, body = adapter.to_upstream(identity, adapter_accepts(adapter, identity))
    ref = uuid.uuid4().hex
    at = utcnow_naive() - timedelta(minutes=minutes_ago)
    async with session_maker() as db:
        db.add(CallRecord(org_id=org_id, user_email="tim@superdesign.dev", tool_name=f"treg.{capability}",
                          method="POST", path="/x", status_code=200, endpoint_id=f"treg.{capability}",
                          provider="treg", credential_tier="routed", call_ref=ref, created_at=at))
        child = CallRecord(org_id=org_id, user_email="tim@superdesign.dev", tool_name=eid, method="POST",
                           path="/x", status_code=200, endpoint_id=eid, provider=a,
                           credential_tier=tier, call_ref=f"{ref}:r1", created_at=at, cached=cached)
        db.add(child)
        if archived:
            key = ArchiveKey(key_hash=f"key-{ref}", endpoint_id=eid,
                             req_url=f"https://api.{a}.example/x?{urlencode(query)}",
                             req_body=json.dumps(body).encode())
            db.add(key)
            await db.flush()
            db.add(ArchiveSnapshot(key_id=key.id, content_hash=f"body-{ref}",
                                   body=json.dumps(answer or {"tavily": TAVILY_HIT, "exa": EXA_HIT}[a]).encode()))
            child.archive_key_hash, child.archive_content_hash = key.key_hash, f"body-{ref}"
        await db.commit()
    if marked:  # the marker holds the input the caller sent, as the call path writes it
        await kv.store().set_text(compare.marker_key(ref), json.dumps(given or {"q": q}),
                                  compare.MARKER_TTL_S)
    return ref


async def _collect() -> dict:
    async with httpx.AsyncClient(trust_env=False) as client:  # the stubbed relay never uses it
        return await compare.collect(client=client)


async def _claim(values: dict, limit: int, team_cap: int = 10) -> str:
    now = utcnow_naive()
    return await compare._claim(session_maker, values, limit, team_cap=team_cap,
                                since=now - timedelta(days=14), ready_since=now - timedelta(days=3))


def _claim_values(org_id: int, call_ref: str) -> dict:
    return dict(org_id=org_id, user_email="x@example.com", call_ref=call_ref, capability="web.search",
                a_endpoint="tavily.web.search", a_provider="tavily", b_provider="exa")


async def _rows() -> list[CallComparison]:
    async with session_maker() as db:
        return list((await db.execute(select(CallComparison).order_by(CallComparison.id))).scalars())


def test_pairs_setting_is_validated_and_parsed():
    assert Settings(compare_pairs="").compare_pair_map == {}
    parsed = Settings(compare_pairs=json.dumps({"web.search": [["provider-a", "provider-b"]]}))
    assert parsed.compare_pair_map == {"web.search": [("provider-a", "provider-b")]}
    for bad in ("[", "[]", '{"web.search": [["a"]]}', '{"web.search": [["a", "a"]]}',
                '{"web.search": [["A", "b"]]}', '{"web.search": "a,b"}'):
        with pytest.raises(ValueError):
            Settings(compare_pairs=bad)
    settings = Settings()
    assert (settings.compare_rate, settings.compare_max_per_day) == (0, 0)
    assert not settings.compare_configured
    assert not Settings(compare_pairs=json.dumps(PAIR)).compare_configured  # no daily limit
    assert Settings(compare_pairs=json.dumps(PAIR), compare_max_per_day=1).compare_configured


def test_a_pair_without_an_adapter_is_skipped(monkeypatch, caplog):
    _compare_on(monkeypatch, pairs={"web.search": [["tavily", "exa"], ["tavily", "no-such-vendor"]],
                                    "no.such.capability": [["tavily", "exa"]]})
    with caplog.at_level("WARNING"):
        assert compare.pairs() == {"web.search": [("tavily", "exa")]}
    assert "no-such-vendor" in caplog.text
    assert compare.other_side("web.search", "exa") == "tavily"
    assert compare.other_side("no.such.capability", "exa") is None


async def test_off_by_default(clients, search_on, monkeypatch):
    sent = _relay_b(monkeypatch)
    await _routed_call(await _org_id(clients), "default off")
    assert (await _collect())["status"] == "off"
    assert sent == [] and await _rows() == []


async def test_an_archive_served_answer_is_not_compared(clients, search_on, monkeypatch):
    """A cached A is stale next to a live B: the parent is skipped and B is never asked."""
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    await _routed_call(org_id, "who makes the best espresso", cached=True)
    await _collect()
    assert sent == [] and await _rows() == []


async def test_a_recent_routed_call_gets_a_ready_pair(clients, search_on, monkeypatch):
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    ref = await _routed_call(org_id, "who makes the best espresso")
    result = await _collect()
    assert (result["status"], result["called"], result["ready"]) == ("ran", 1, 1)
    # B is asked once, directly on its own endpoint, with the request its own adapter builds.
    call, = sent
    assert urlsplit(call["url"]).hostname == "api.exa.ai" and call["tool"].name == "exa.web.search"
    assert call["body"]["query"] == "who makes the best espresso"
    assert all(b.get("platform_setting") for b in call["tool"].bindings)
    row, = await _rows()
    assert (row.status, row.call_ref, row.capability) == ("ready", ref, "web.search")
    assert (row.a_endpoint, row.a_provider, row.b_endpoint, row.b_provider) == (
        "tavily.web.search", "tavily", "exa.web.search", "exa")
    assert row.b_cost_micro == result["estimate_micro"] > 0
    a_out, b_out = json.loads(row.a_body), json.loads(row.b_body)
    assert set(a_out) == set(b_out) and a_out != b_out
    assert "[vendor] says" in row.b_body and "[vendor] says" in row.a_body
    assert json.loads(row.input)["q"] == "who makes the best espresso"
    assert row.truncated == "" and row.first in ("a", "b")
    assert await kv.store().has_flag(compare.ready_key(org_id))
    # The same call is never asked twice.
    assert (await _collect())["called"] == 0 and len(sent) == 1


@pytest.mark.parametrize("case", ["opt_out", "demo", "deny_rule", "tool_acl", "pinned_member",
                                  "capability_pin", "not_in_pair", "own_key", "not_archived",
                                  "too_old", "no_marker", "store_down", "suspended", "paused",
                                  "flow_unavailable", "flow_raises", "flow_invalid"])
async def test_calls_that_are_never_compared(clients, search_on, monkeypatch, case):
    _compare_on(monkeypatch, pairs={"web.search": [["tavily", "parallel"]]} if case == "not_in_pair" else PAIR)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        if case == "opt_out":
            org.archive_opt_out_at = utcnow_naive()
        elif case == "demo":
            org.demo = True
        elif case == "deny_rule":
            db.add(DenyRule(org_id=org_id, host="unrelated.example", note="any rule"))
        elif case in ("tool_acl", "pinned_member"):
            member = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().first()
            if case == "tool_acl":
                member.tool_access = ["some.tool"]
            else:
                member.pinned_tags = {"customer": "c1"}
        elif case == "capability_pin":
            db.add(CapabilityPin(org_id=org_id, capability="web.search", provider="tavily"))
        await db.commit()
    if case == "store_down":
        async def unknown(key):
            return None  # what `get_text` answers when the store cannot
        monkeypatch.setattr(kv.store(), "get_text", unknown)
    late = ("suspended", "paused", "flow_unavailable", "flow_raises", "flow_invalid")
    if case in late:
        # Revoked after the run picked the call, before it is sent: checked again right then.
        real = compare._b_target

        def then_revoke(*a, **kw):
            nonlocal_state.append(True)
            return real(*a, **kw)
        nonlocal_state: list = []
        monkeypatch.setattr(compare, "_b_target", then_revoke)
        if case == "suspended":
            async with session_maker() as db:
                (await db.get(Org, org_id)).suspended = True
                await db.commit()
        else:
            # The flow-control read itself, from the store: a pause, or any state it cannot confirm.
            monkeypatch.setattr(get_settings(), "org_flow_control_enabled", True)
            pause = (utcnow_naive() + timedelta(hours=1)).isoformat(timespec="seconds") + "Z"
            published = json.dumps({"org_id": org_id, "version": 1, "config": {
                "schema_version": 1, "pause_until": pause, "rate_limit": None, "concurrency_limit": None}})

            async def read(org):
                if case == "flow_raises":
                    raise ConnectionError("store down")
                return {"paused": published, "flow_invalid": "{not json",
                        "flow_unavailable": kv.FlowControlReadFailure.UNAVAILABLE}[case]
            monkeypatch.setattr(kv.store(), "read_org_flow_control", read)
    await _routed_call(org_id, f"never {case}", tier="credential" if case == "own_key" else "platform",
                       archived=case != "not_archived", minutes_ago=600 if case == "too_old" else 5,
                       marked=case != "no_marker")
    result = await _collect()
    assert sent == [] and await _rows() == [], result
    # A candidate that reached `_one` is counted under the reason it stopped at.
    expected = {"not_in_pair": "b_unavailable", "own_key": "a_not_on_treg_key",
                "not_archived": "a_not_archived", "no_marker": "no_marker",
                "store_down": "no_marker", "suspended": "team_no_longer_eligible",
                "paused": "team_no_longer_eligible", "flow_unavailable": "team_no_longer_eligible",
                "flow_raises": "team_no_longer_eligible", "flow_invalid": "team_no_longer_eligible",
                }.get(case)
    if expected:
        assert result["skip_reasons"] == {expected: 1}, result
    else:
        assert result["skip_reasons"] == {}, result
    if case in late:
        assert nonlocal_state, "the call got as far as the last check"


async def test_per_run_and_per_day_limits(clients, search_on, monkeypatch):
    _compare_on(monkeypatch, compare_max_per_run=2, compare_max_per_day=3)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    for i in range(4):
        await _routed_call(org_id, f"question {i}")
    assert (await _collect())["called"] == 2
    assert (await _collect())["called"] == 1  # the third of the day
    assert (await _collect())["status"] == "at_limit"
    assert len(sent) == 3 and len(await _rows()) == 3


async def test_team_and_pair_cap_stops_sampling(clients, search_on, monkeypatch):
    _compare_on(monkeypatch, compare_team_cap=2)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    async with session_maker() as db:
        for i, (a, b) in enumerate((("tavily", "exa"), ("exa", "tavily"))):
            db.add(CallComparison(org_id=org_id, user_email="x@example.com", call_ref=f"old-{i}",
                                  capability="web.search", a_endpoint=f"{a}.web.search", a_provider=a,
                                  b_provider=b, status="decided", choice="a"))
        await db.commit()
    await _routed_call(org_id, "team has enough")
    await _collect()
    assert sent == [] and len(await _rows()) == 2


@pytest.mark.parametrize("failure", [{"status": 502}, {"status": 0},
                                     {"body": {"results": [], "costDollars": {"total": 0}}}])
async def test_a_b_failure_calls_nothing_else(clients, search_on, monkeypatch, failure):
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch, **failure)
    org_id = await _org_id(clients)
    await _routed_call(org_id, f"b fails {sorted(failure)}")
    result = await _collect()
    assert len(sent) == 1 and sent[0]["tool"].name == "exa.web.search"
    row, = await _rows()
    assert (row.status, row.a_body, row.b_body, row.input) == ("failed", "", "", "")
    assert result["failed"] == 1 and not await kv.store().has_flag(compare.ready_key(org_id))


async def test_b_is_skipped_without_its_key_paused_or_exhausted(clients, search_on, monkeypatch):
    from treg.application.call import keyslots

    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tavily")
    get_settings.cache_clear()
    _compare_on(monkeypatch)
    await _routed_call(org_id, "exa has no key")
    await _collect()
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tavily,exa")
    monkeypatch.setenv("TREG_PAUSED_PROVIDERS", "exa")
    get_settings.cache_clear()
    _compare_on(monkeypatch)
    await _routed_call(org_id, "exa is paused")
    await _collect()
    monkeypatch.delenv("TREG_PAUSED_PROVIDERS")
    get_settings.cache_clear()
    _compare_on(monkeypatch)
    monkeypatch.setattr(keyslots, "exhausted", lambda service, *a, **kw: service == "exa")
    await _routed_call(org_id, "exa is out")
    await _collect()
    assert sent == [] and await _rows() == []


async def test_a_judge_only_adapter_never_builds_b(clients, search_on, monkeypatch):
    """A `route: false` adapter only judges its endpoint's own answers; the router never sends the
    request it would build (cloro's Google organic one has no `gl`, which cloro requires), so it is
    never B."""
    import dataclasses

    cat = catalog_store.load()
    monkeypatch.setitem(cat.adapters, "exa.web.search",
                        dataclasses.replace(cat.adapters["exa.web.search"], route=False))
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    await _routed_call(await _org_id(clients), "exa is not routed")
    result = await _collect()
    assert sent == [] and await _rows() == []
    assert result["skip_reasons"] == {"b_unavailable": 1}
    monkeypatch.setenv("TREG_PLATFORM_KEY_CLORO", "PLATFORM-CLORO")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "cloro")
    get_settings.cache_clear()
    contract = cat.contracts["google.serp.organic"]
    identity, _ = canonical_identity(contract, {"q": "espresso grinders"})
    assert cat.adapters["cloro.google.serp.organic"].route is False
    assert compare._b_target(contract, identity, "cloro") is None


# B's request is the router's: the same canonical identity sent as a routed call on treg's key and
# asked of B by the run reach the vendor as the same bytes (credentials masked).
B_ENDPOINTS = [
    ("keenable", "keenable.web.fetch", "web.extract", {"url": "https://docs.example.com/guide"}),
    ("dataforseo", "dataforseo.google.serp.organic", "google.serp.organic", {"q": "espresso grinders"}),
    ("crawl4ai", "crawl4ai.web.search", "web.search", {"q": "espresso grinders"}),
    ("parallel", "parallel.web.search", "web.search", {"q": "espresso grinders"}),
    ("exa", "exa.web.search", "web.search", {"q": "espresso grinders"}),
    ("anyapi", "anyapi.google.serp.organic", "google.serp.organic", {"q": "espresso grinders"}),
]
_SECRET_HEADERS = {"authorization", "x-api-key"}


def _on_the_wire(request: httpx.Request, content: bytes) -> dict:
    return {"method": request.method, "url": str(request.url), "body": content.decode(),
            "headers": sorted((k, "***" if k in _SECRET_HEADERS else v)
                              for k, v in request.headers.multi_items())}


def _vendor(seen: list, answer: bytes = b"{}"):
    async def vendor(request: httpx.Request) -> httpx.Response:
        seen.append(_on_the_wire(request, await request.aread()))
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(answer))
    return httpx.AsyncClient(transport=httpx.MockTransport(vendor))


def _platform(monkeypatch, *providers):
    for p in providers:
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p.upper()}", f"PLATFORM-{p.upper()}")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ",".join(providers))
    monkeypatch.setenv("TREG_CLIENT_ID_SECRET", "client-id-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", False)


@pytest.mark.parametrize("provider,endpoint,capability,given", B_ENDPOINTS)
async def test_b_sends_what_the_router_sends(clients, monkeypatch, provider, endpoint, capability, given):
    from treg.infra.upstream.relay import relay as real_relay

    _platform(monkeypatch, provider)
    seen = []
    async with _vendor(seen) as vendor:
        async def relay(request, url, tool, secrets, client, **kw):
            return await real_relay(request, url, tool, secrets, vendor, **kw)
        monkeypatch.setattr(call_service, "relay", relay)
        monkeypatch.setattr(compare, "relay", relay)
        org_id = await _org_id(clients)
        contract = catalog_store.load().contracts[capability]
        identity, _ = canonical_identity(contract, given)
        target = compare._b_target(contract, identity, provider)
        assert target is not None and target.ep["id"] == endpoint
        assert (await compare._ask_b(target, vendor, org_id))[0] == 200
        routed = await clients.post(f"/call/treg.{capability}", json=given,
                                    headers={"X-Treg-Route-Prefer": endpoint})
    assert routed.status_code == 200, routed.text
    assert routed.json()["_treg"]["tried"][0]["endpoint_id"] == endpoint
    b, child = seen[:2]
    assert b == child
    # What the run once sent differently: Python's `False` in the query, no client header, no ceiling.
    if provider == "keenable":
        assert "live=false" in b["url"]
    if provider == "crawl4ai":
        assert dict(b["headers"])["x-c4-client"].startswith("t_")
    if provider == "anyapi":
        assert "max_cost_usd=" in b["url"]


def _example(endpoint_id: str) -> bytes:
    return catalog_store.example_path(endpoint_id).read_bytes()


# A configured pair's A and B, each answering with its captured example: stored `ready`.
READY_PAIRS = [
    ("google.serp.organic", "anyapi", "dataforseo", {"q": "espresso grinders"}),
    ("web.extract", "crawl4ai", "keenable", {"url": "https://docs.example.com/guide"}),
    ("web.search", "exa", "crawl4ai", {"q": "espresso grinders"}),
    ("web.search", "crawl4ai", "exa", {"q": "espresso grinders"}),
    ("web.search", "tinyfish", "parallel", {"q": "espresso grinders"}),
]


@pytest.mark.parametrize("capability,a,b,given", READY_PAIRS)
async def test_a_vendor_example_answer_is_stored_ready(clients, monkeypatch, capability, a, b, given):
    _platform(monkeypatch, a, b)
    _compare_on(monkeypatch, pairs={capability: [[a, b]]})
    cat = catalog_store.load()
    a_endpoint = next(e["id"] for e in cat.for_capability(capability)
                      if e["provider"] == a and e["id"] in cat.adapters)
    b_endpoint = next(e["id"] for e in cat.for_capability(capability)
                      if e["provider"] == b and e["id"] in cat.adapters)
    org_id = await _org_id(clients)
    await _routed_call(org_id, given.get("q", ""), a=a, capability=capability, given=given,
                       answer=json.loads(_example(a_endpoint)))
    seen = []
    async with _vendor(seen, _example(b_endpoint)) as vendor:
        result = await compare.collect(client=vendor)
    assert (result["called"], result["ready"]) == (1, 1), result
    row, = await _rows()
    assert (row.status, row.b_endpoint) == ("ready", b_endpoint)
    assert json.loads(row.b_body) and json.loads(row.a_body)


async def test_an_off_question_answer_is_failed_as_the_router_reads_it(clients, monkeypatch, caplog):
    """A `site:` question answered with no row on that site is the router's miss (`question_miss`),
    so the pair fails even on a 200: why a DataForSEO answer to such a question was never a hit."""
    _platform(monkeypatch, "anyapi", "dataforseo")
    _compare_on(monkeypatch, pairs={"google.serp.organic": [["anyapi", "dataforseo"]]})
    org_id = await _org_id(clients)
    given = {"q": "espresso grinders site:nowhere.example"}
    a_answer = json.loads(_example("anyapi.google.serp.organic"))
    for row in a_answer["output"]["data"]["results"]:
        row["url"] = "https://nowhere.example/grinders"
    await _routed_call(org_id, given["q"], a="anyapi", capability="google.serp.organic", given=given,
                       answer=a_answer)
    caplog.set_level("INFO", logger="treg.application.compare")
    async with _vendor([], _example("dataforseo.google.serp.organic")) as vendor:
        result = await compare.collect(client=vendor)
    assert (result["called"], result["failed"]) == (1, 1), result
    assert (await _rows())[0].status == "failed"
    assert "status 200, read as miss (question_miss)" in caplog.text


async def test_a_team_deleted_during_the_run_gets_no_row(clients, search_on, monkeypatch):
    from treg.domain.governance import teams

    _compare_on(monkeypatch)
    org_id = await _org_id(clients)

    async def delete_team():
        async with session_maker() as db:
            await teams.cascade_delete_org(await db.get(Org, org_id), db)
            await db.commit()
    sent = _relay_b(monkeypatch, before=delete_team)
    await _routed_call(org_id, "team leaves")
    await _collect()
    assert len(sent) == 1 and await _rows() == []


async def test_a_row_settled_as_the_team_goes_is_taken_back(clients, monkeypatch):
    from contextlib import asynccontextmanager

    org_id = await _org_id(clients)
    values = dict(org_id=org_id, user_email="x@example.com", call_ref="racing", capability="web.search",
                  a_endpoint="tavily.web.search", a_provider="tavily", b_provider="exa")
    assert await _claim(values, 5) == "claimed"
    opened = [0]

    @asynccontextmanager
    async def sessions():
        opened[0] += 1
        if opened[0] == 2:  # the team row goes between the settle and the look after it
            async with session_maker() as db:
                from sqlalchemy import text
                await db.execute(text("DELETE FROM membership WHERE org_id = :o"), {"o": org_id})
                await db.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})
                await db.commit()
        async with session_maker() as db:
            yield db
    assert not await compare._finish(sessions, org_id, "racing", {"status": "failed"})
    assert await _rows() == []


async def test_concurrent_runs_send_once(clients, search_on, monkeypatch):
    import asyncio

    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    await _routed_call(await _org_id(clients), "two runs at once")
    await asyncio.gather(_collect(), _collect())
    assert len(sent) == 1 and len(await _rows()) == 1


async def test_a_claim_left_by_a_crash_is_never_resent_and_still_counts(clients, search_on, monkeypatch):
    _compare_on(monkeypatch, compare_max_per_day=1)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    ref = await _routed_call(org_id, "crashed after the claim")
    values = dict(org_id=org_id, user_email="x@example.com", call_ref=ref, capability="web.search",
                  a_endpoint="tavily.web.search", a_provider="tavily", b_provider="exa")
    assert await _claim(values, 1) == "claimed"
    await _routed_call(org_id, "the next call")
    assert (await _collect())["status"] == "at_limit"
    assert sent == [] and [r.status for r in await _rows()] == ["pending"]


async def test_deleting_a_team_gives_no_quota_back(clients, search_on, monkeypatch):
    from treg.domain.governance import teams

    _compare_on(monkeypatch, compare_max_per_day=1)
    sent = _relay_b(monkeypatch)
    other = (await clients.post("/orgs", json={"name": "Leaving team"})).json()
    await _routed_call(other["org_id"], "the day's one call")
    assert (await _collect())["called"] == 1
    async with session_maker() as db:
        await teams.cascade_delete_org(await db.get(Org, other["org_id"]), db)
        await db.commit()
    assert await _rows() == []
    await _routed_call(await _org_id(clients), "after the deletion")
    assert (await _collect())["status"] == "at_limit" and len(sent) == 1


@pytest.mark.parametrize("off", [{}, {"compare_pairs": ""}, {"compare_max_per_day": 0}])
async def test_rows_of_a_deleted_team_are_removed_by_every_run(clients, search_on, monkeypatch, off):
    _compare_on(monkeypatch, **off)
    _relay_b(monkeypatch)
    async with session_maker() as db:
        db.add(CallComparison(org_id=987654, user_email="gone@example.com", call_ref="orphan",
                              capability="web.search", a_endpoint="tavily.web.search", a_provider="tavily",
                              b_provider="exa"))
        await db.commit()
    assert (await _collect())["removed"] == 1
    assert await _rows() == []


def test_blinding_keeps_the_text_and_hides_the_vendor():
    pattern = compare._vendor_pattern(["tavily", "exa"], ["tavily.web.search", "exa.web.search"])
    output = {"results": [{"title": "Exa says: see https://api.exa.ai/docs", "provider": "exa",
                           "source": "exa", "meta": {"source": "City paper", "via": "exa.web.search",
                                                      "next_cursor": "trg1.abc", "n": 3},
                           "note": "an example from Tavily, not tavilyx"}],
              "next_cursor": "trg1.xyz", "_treg": {"served_by": "exa.web.search"}}
    assert compare._blind(output, pattern, {"tavily", "exa"}) == {"results": [{
        "title": "[vendor] says: see https://[vendor]/docs",
        "meta": {"source": "City paper", "via": "[vendor]", "n": 3},
        "note": "an example from [vendor], not tavilyx"}]}
    text, cut = compare._truncate("word " * 400, 1024)
    assert cut and len(text.encode()) == 1024


# The invitation

def _flagged_call(monkeypatch):
    from test_hints import _platform_call
    _platform_call(monkeypatch)
    for field in ("review_sample_rate", "feedback_hint_rate", "mark_hint_rate"):
        monkeypatch.setattr(get_settings(), field, 0)


async def test_a_waiting_pair_invites_a_client_that_shows_compare(clients, monkeypatch):
    _compare_on(monkeypatch)
    _flagged_call(monkeypatch)
    org_id = await _org_id(clients)
    assert (await clients.get("/call/example.search", headers={"X-Treg-Hints": "review,mark,feedback,compare"})
            ).headers.get("X-Treg-Hint") is None
    await kv.store().set_flag(compare.ready_key(org_id), 60)
    shown = await clients.get("/call/example.search", headers={"X-Treg-Hints": "review,mark,feedback,compare"})
    assert shown.headers["X-Treg-Hint"] == "compare"
    assert shown.headers["X-Treg-Hint-Text"] == hints.COMPARE_CLI_HINT
    # A CLI says nothing (legacy kinds) or names its kinds without compare: never invited.
    assert (await clients.get("/call/example.search")).headers.get("X-Treg-Hint") is None
    assert (await clients.get("/call/example.search", headers={"X-Treg-Hints": "review,mark,feedback"})
            ).headers.get("X-Treg-Hint") is None


async def test_compare_invitations_are_rationed_and_review_falls_through(clients, monkeypatch):
    _compare_on(monkeypatch, compare_budget_per_hour=1)
    _flagged_call(monkeypatch)
    monkeypatch.setattr(get_settings(), "review_sample_rate", 1)
    await kv.store().set_flag(compare.ready_key(await _org_id(clients)), 60)
    kinds = [(await clients.get("/call/example.search", headers={"X-Treg-Hints": "review,compare"})
              ).headers.get("X-Treg-Hint") for _ in range(2)]
    assert kinds == ["compare", "review"]


async def test_team_mcp_renders_the_compare_hint_and_the_directory_never_asks(clients, monkeypatch):
    from test_marketplace_call import _fake_relay
    from test_mcp_directory import _call_tool, paired_mcp_session

    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "SYNTHETIC-PLATFORM-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    try:
        _compare_on(monkeypatch)
        monkeypatch.setattr(hints, "sampled", lambda kind, ref: False)
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data":{"items":[]}}'))
        org_id = await _org_id(clients)
        await kv.store().set_flag(compare.ready_key(org_id), 60)
        cid = await _seed(org_id, first="b", call_ref="inline-pair")
        args = {"endpoint_id": "tikhub.tiktok.video.comments", "params": {"aweme_id": "7"}}
        async with paired_mcp_session() as client:
            team = await _call_tool(client, "call", {**args, "idempotency_key": "compare-team"},
                                    clients.headers["X-Treg-Token"], path="/mcp/")
            directory = await _call_tool(client, "catalog_call_read", {**args, "idempotency_key": "compare-v2"},
                                         clients.headers["X-Treg-Token"])
        # The team MCP carries the pair itself, blinded, and names the comparison to answer.
        assert team["compare"] == {"comparison_id": cid, "capability": "web.search", "input": '{"q":"x"}',
                                   "answer_1": '"answer B"', "answer_2": '"answer A"'}
        assert team["hint"] == hints.compare_inline_hint(cid)
        assert "cut when stored" not in team["hint"]
        assert "hint" not in directory and "compare" not in directory
    finally:
        get_settings.cache_clear()


async def test_team_mcp_without_a_waiting_pair_carries_no_invitation(clients, monkeypatch):
    """The flag can outlive its pair (decided elsewhere, or aged out): then no hint and no pair."""
    from test_marketplace_call import _fake_relay
    from test_mcp_directory import _call_tool, paired_mcp_session

    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "SYNTHETIC-PLATFORM-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    try:
        _compare_on(monkeypatch)
        monkeypatch.setattr(hints, "sampled", lambda kind, ref: False)
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data":{"items":[]}}'))
        await kv.store().set_flag(compare.ready_key(await _org_id(clients)), 60)
        args = {"endpoint_id": "tikhub.tiktok.video.comments", "params": {"aweme_id": "7"},
                "idempotency_key": "compare-none"}
        async with paired_mcp_session() as client:
            team = await _call_tool(client, "call", args, clients.headers["X-Treg-Token"], path="/mcp/")
        assert "compare" not in team and "hint" not in team
    finally:
        get_settings.cache_clear()


def test_an_inline_answer_keeps_its_first_rows_and_cuts_long_text():

    rows = [{"title": f"t{i}", "snippet": "s" * 1000} for i in range(12)]
    text, cut = compare.compact_answer(json.dumps({"results": rows, "total": 12}))
    shown = json.loads(text)
    assert cut is True
    assert [r["title"] for r in shown["results"]] == ["t0", "t1", "t2", "t3", "t4"]
    assert len(shown["results"][0]["snippet"]) == compare.INLINE_FIELD_CHARS + 1
    assert shown["total"] == 12
    assert compare.compact_answer('{"results": [{"title": "short"}]}') == ('{"results":[{"title":"short"}]}', False)
    long_text = "not json " * 1000
    assert compare.compact_answer(long_text) == (long_text[:compare.INLINE_ANSWER_CHARS], True)
    assert compare.compact_answer(None) == ("", False)


async def test_an_inline_pair_keeps_its_truncation_and_says_when_the_input_was_cut(clients, monkeypatch):
    """A stored cut (`truncated_parts`) travels with the pair, and an incomplete input changes the
    hint, so the agent does not judge a filter it cannot see."""
    from treg import mcp as mcp_module

    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    cid = await _seed(org_id, first="a", call_ref="cut-pair", truncated="input,b")
    pair = await mcp_module._inline_comparison(clients.headers["X-Treg-Token"])
    assert pair["comparison_id"] == cid
    assert sorted(pair["truncated_parts"]) == ["answer_2", "input"]
    hint = hints.compare_inline_hint(cid, input_cut="input" in pair["truncated_parts"])
    assert "cut when stored" in hint


async def test_an_identity_token_reads_its_pair_through_the_team_the_call_resolved(clients, monkeypatch):
    """A `treg login` identity token names no team: the inline read and the compare tool carry the
    team `_resolve_org` found, as the call itself does, or the API answers 400 and drops the pair."""
    from conftest import verified_signup
    from test_mcp import _call_tool, mcp_session

    from treg import mcp as mcp_module

    _compare_on(monkeypatch)
    token = (await verified_signup(clients, json={"email": "identity-compare@superdesign.dev"})).json()["token"]
    team = (await clients.get("/orgs", headers={"X-Treg-Token": token})).json()[0]
    cid = await _seed(team["org_id"], first="a", call_ref="identity-pair")
    assert (await mcp_module._inline_comparison(token, team["slug"]))["comparison_id"] == cid
    # The slug is what the read asks with: a team this person is not in finds nothing.
    assert await mcp_module._inline_comparison(token, "not-their-team") is None
    async with mcp_session(clients) as c:
        shown = await _call_tool(c, "compare", {}, token)
        sent = await _call_tool(c, "compare", {"comparison_id": cid, "choice": "1"}, token)
    assert shown["comparison_id"] == cid and sent["status"] == "received"


async def test_a_slow_comparison_read_never_holds_the_call(clients, monkeypatch):
    """The call has answered and been charged: a hung read is dropped after its short limit."""
    import asyncio

    from treg import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "INLINE_READ_TIMEOUT_S", 0.05)
    cancelled = []

    async def hang(*_a, **_k):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(compare, "next_for", hang)
    _compare_on(monkeypatch)
    await _seed(await _org_id(clients), first="a", call_ref="slow-pair")
    # A wall-clock bound flakes on a slow runner; the hung read being cancelled is the behavior.
    assert await asyncio.wait_for(mcp_module._inline_comparison(clients.headers["X-Treg-Token"]), 30) is None
    assert cancelled == [True]


# The API and the MCP tool

async def _seed(org_id: int, *, first: str, call_ref: str, age_days: float = 0, status: str = "ready",
                truncated: str = ""):
    async with session_maker() as db:
        row = CallComparison(org_id=org_id, user_email="tim@superdesign.dev", call_ref=call_ref,
                             capability="web.search", a_endpoint="provider-a.web.search",
                             a_provider="provider-a", b_endpoint="provider-b.web.search",
                             b_provider="provider-b", input='{"q":"x"}', a_body='"answer A"',
                             b_body='"answer B"', first=first, status=status, truncated=truncated,
                             created_at=utcnow_naive() - timedelta(days=age_days))
        db.add(row)
        await db.commit()
        return row.id


async def test_next_is_blinded_and_a_verdict_maps_back(clients, monkeypatch):
    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    await _seed(org_id, first="a", call_ref="too-old", age_days=4)
    await _seed(org_id, first="b", call_ref="failed", status="failed")
    one = await _seed(org_id, first="b", call_ref="first-b")
    two = await _seed(org_id, first="a", call_ref="first-a")
    await kv.store().set_flag(compare.ready_key(org_id), 60)

    got = await clients.get("/comparisons/next")
    assert got.status_code == 200, got.text
    assert got.json() == {"comparison_id": one, "capability": "web.search", "input": '{"q":"x"}',
                          "answer_1": '"answer B"', "answer_2": '"answer A"', "truncated": False}
    assert "provider" not in got.text and "_treg" not in got.text

    sent = await clients.post("/comparisons", json={"comparison_id": one, "choice": "1", "reason": "B is right"},
                              headers={"X-Treg-Client": "compare-test"})
    assert sent.status_code == 201 and sent.json() == {"comparison_id": one, "choice": "1", "status": "received"}
    again = await clients.post("/comparisons", json={"comparison_id": one, "choice": "2"})
    assert again.status_code == 200 and again.json() == {"comparison_id": one, "choice": "1",
                                                         "status": "already_received"}
    assert await kv.store().has_flag(compare.ready_key(org_id))  # the second is still waiting

    assert (await clients.get("/comparisons/next")).json()["comparison_id"] == two
    assert (await clients.post("/comparisons", json={"comparison_id": two, "choice": "1"})).status_code == 201
    assert (await clients.get("/comparisons/next")).status_code == 204
    assert not await kv.store().has_flag(compare.ready_key(org_id))

    rows = {r.call_ref: r for r in await _rows()}
    assert (rows["first-b"].choice, rows["first-b"].reason, rows["first-b"].client) == ("b", "B is right", "compare-test")
    assert rows["first-a"].choice == "a" and rows["first-a"].status == "decided"
    assert rows["first-b"].decided_at is not None and rows["too-old"].status == "ready"


async def test_tie_and_both_bad_are_kept_as_said(clients, monkeypatch):
    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    tie = await _seed(org_id, first="b", call_ref="tie")
    bad = await _seed(org_id, first="a", call_ref="bad")
    assert (await clients.post("/comparisons", json={"comparison_id": tie, "choice": "tie"})).json()["choice"] == "tie"
    assert (await clients.post("/comparisons", json={"comparison_id": bad, "choice": "both_bad"})).json()["choice"] == "both_bad"
    assert {r.call_ref: r.choice for r in await _rows()} == {"tie": "tie", "bad": "both_bad"}
    assert (await clients.post("/comparisons", json={"comparison_id": tie, "choice": "3"})).status_code == 422


async def test_another_team_can_neither_read_nor_answer(clients, monkeypatch):
    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    mine = await _seed(org_id, first="a", call_ref="mine")
    failed = await _seed(org_id, first="a", call_ref="mine-failed", status="failed")
    other = (await clients.post("/orgs", json={"name": "Other team"})).json()
    headers = {"X-Treg-Token": other["token"]}
    assert (await clients.get("/comparisons/next", headers=headers)).status_code == 204
    assert (await clients.post("/comparisons", json={"comparison_id": mine, "choice": "1"},
                               headers=headers)).status_code == 404
    assert (await clients.post("/comparisons", json={"comparison_id": failed, "choice": "1"})).status_code == 404
    assert (await _rows())[0].status == "ready"


async def test_mcp_compare_tool_round_trip(clients, monkeypatch):
    from test_mcp import _call_tool, mcp_session

    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    token = clients.headers["X-Treg-Token"]
    cid = await _seed(org_id, first="b", call_ref="mcp-pair")
    async with mcp_session(clients) as c:
        shown = await _call_tool(c, "compare", {}, token)
        assert (shown["comparison_id"], shown["answer_1"], shown["answer_2"]) == (cid, '"answer B"', '"answer A"')
        assert "compare(comparison_id" in shown["instruction"]
        missing = await _call_tool(c, "compare", {"choice": "1"}, token)
        assert missing["status"] == "error"
        sent = await _call_tool(c, "compare", {"comparison_id": cid, "choice": "2", "reason": "A cites"}, token)
        assert sent["status"] == "received" and sent["choice"] == "2"
        none = await _call_tool(c, "compare", {}, token)
        assert none["status"] == "none"
    row, = await _rows()
    assert (row.choice, row.reason, row.client) == ("a", "A cites", "mcp")


async def test_local_flags_expire(monkeypatch):
    from treg.infra import kv as kv_module

    now = [100.0]
    monkeypatch.setattr(kv_module.time, "monotonic", lambda: now[0])
    store = kv_module.LocalStore()
    assert await store.set_flag("f", 10) and await store.has_flag("f")
    await store.clear_flag("f")
    assert not await store.has_flag("f")
    await store.set_flag("f", 10)
    now[0] += 11
    assert not await store.has_flag("f")


async def test_a_cut_answer_is_marked_in_shown_order(clients, monkeypatch):
    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    await _seed(org_id, first="b", call_ref="cut", truncated="input,a")
    got = (await clients.get("/comparisons/next")).json()
    assert got["truncated"] is True and got["truncated_parts"] == ["input", "answer_2"]


async def test_a_pair_stored_while_the_flag_is_cleared_keeps_its_invitation(clients, monkeypatch):
    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    store = kv.store()
    real_clear = store.clear_flag

    async def clear_then_a_run_lands(key):
        await real_clear(key)
        await _seed(org_id, first="a", call_ref="landed")
    monkeypatch.setattr(store, "clear_flag", clear_then_a_run_lands)
    assert (await clients.get("/comparisons/next")).status_code == 204
    assert await store.has_flag(compare.ready_key(org_id))


async def test_the_worker_command_runs_one_collect(clients, monkeypatch, capsys):
    import argparse

    import treg.infra.db as infra_db
    from treg import worker

    async def verified():
        return None
    monkeypatch.setattr(infra_db, "verify_db", verified)
    asked = []

    async def collect(**kw):
        asked.append(kw)
        return {"status": "off"}
    monkeypatch.setattr(compare, "collect", collect)
    assert await worker._compare_collect(argparse.Namespace(max_seconds=5.0)) == 0
    assert asked == [{"max_seconds": 5.0}] and json.loads(capsys.readouterr().out) == {"status": "off"}



# The call path's marker

@pytest.mark.parametrize("header", [None, "X-Treg-Route-Exclude", "X-Treg-Route-Prefer",
                                    "X-Treg-Route-Max-Cost", "X-Treg-Route-Waterfall", "X-Treg-Route-Anything"])
async def test_a_routed_call_is_marked_only_without_route_headers(clients, search_on, monkeypatch, header):
    from test_routing import _relay_by_provider

    _compare_on(monkeypatch)
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tavily": [(200, TAVILY_HIT)] * 3, "exa": [(200, EXA_HIT)] * 3}, []))
    value = {"X-Treg-Route-Exclude": "parallel", "X-Treg-Route-Prefer": "tavily,exa",
             "X-Treg-Route-Max-Cost": "0.5", "X-Treg-Route-Waterfall": "1"}.get(header, "x")
    response = await clients.post("/call/treg.web.search", json={"q": f"marked {header}"},
                                  headers={header: value} if header else {})
    assert response.status_code == 200, response.text
    assert "compare" not in response.text
    marker = await kv.store().get_text(compare.marker_key(response.headers["X-Treg-Call-Id"]))
    if header is None:  # the marker holds the input the caller sent, nothing else
        assert json.loads(marker) == {"q": "marked None"}
    else:
        assert marker is None


async def test_a_team_that_never_reviews_is_not_compared(clients, search_on, monkeypatch):
    """Its agents would leave the pair unjudged: no call is made and it is not counted eligible."""
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    await _routed_call(org_id, "never reviews", reviewing=False)
    result = await _collect()
    assert (result["eligible"], result["called"], sent) == (0, 0, []), result


@pytest.mark.parametrize("hints_sent,compared", [
    ("review,mark,feedback,compare", True),   # a release that shows compare
    ("review,mark,feedback", False),          # an older release under the same agent name
    (None, False),                            # the Python CLI
])
async def test_a_cli_team_is_compared_only_once_a_client_showing_compare_answered(
        clients, search_on, monkeypatch, hints_sent, compared):
    """A CLI reports only the agent it runs under, so what counts is whether the client that
    answered the invitation declared it shows compare."""
    from treg.application import review_targets

    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    monkeypatch.setattr(review_targets, "sampled", lambda *_: True)
    org_id = await _org_id(clients)
    reviewed = await _routed_call(org_id, "reviewed on the CLI", reviewing=False, marked=False)
    headers = {"X-Treg-Client": "claude-code", **({"X-Treg-Hints": hints_sent} if hints_sent else {})}
    got = await clients.post("/reviews", json={"call_id": reviewed, "usefulness": "useful"}, headers=headers)
    assert got.status_code == 201, got.text
    await _routed_call(org_id, "answers on the CLI", reviewing=False)
    result = await _collect()
    assert result["called"] == (1 if compared else 0), result
    assert bool(sent) is compared


async def test_a_declared_answer_counts_only_inside_the_current_window(clients, search_on, monkeypatch):
    """The note holds when a client that shows compare last answered, so shortening
    `compare_answer_days` drops a team whose only such answer is older than the new window."""
    _compare_on(monkeypatch, compare_answer_days=1)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    async with session_maker() as db:
        db.add(CallReview(org_id=org_id, user_email="tim@superdesign.dev", call_id="reviewed",
                          endpoint_id="tavily.web.search", usefulness="useful", invited=True,
                          client="claude-code"))  # today, from an older CLI
        await db.commit()
    await kv.store().set_text(compare.answering_key(org_id),
                              (utcnow_naive() - timedelta(days=4)).isoformat(), 7 * 86400)
    await _routed_call(org_id, "answers on an old CLI", reviewing=False)
    assert (await _collect())["eligible"] == 0
    assert sent == []


async def test_a_compact_pair_carries_the_cli_words(clients, monkeypatch):
    """What a CLI prints beside a call: the compacted pair and the words asking for its verdict."""
    _compare_on(monkeypatch)
    org_id = await _org_id(clients)
    cid = await _seed(org_id, first="a", call_ref="cli-pair")
    got = await clients.get("/comparisons/next", params={"compact": "true"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["comparison_id"] == cid and body["answer_1"] == '"answer A"'
    assert body["how_to_vote"] == hints.compare_cli_hint(cid)
    assert f"treg compare {cid} 1|2|tie|both_bad" in body["how_to_vote"]
    assert "provider" not in got.text and "truncated" not in body
    assert "how_to_vote" not in (await clients.get("/comparisons/next")).json()


@pytest.mark.parametrize("client,invited,days_ago,compared", [
    ("mcp", True, 1, True),         # answered an invitation on the team MCP
    ("mcp-prior", True, 1, True),   # a verdict riding on the next MCP call
    ("mcp", False, 1, False),       # reviewed unasked: says nothing about reading hints
    ("claude-code", True, 1, False),  # a CLI answer that never declared it shows compare
    ("mcp", True, 10, False),       # answered, but not in the last week
])
async def test_only_a_team_answering_mcp_invitations_is_compared(clients, search_on, monkeypatch,
                                                                 client, invited, days_ago, compared):
    """Only the team MCP shows a compare invitation, so a team is compared only when its agent
    answered a review invitation there recently; a pipeline that never reads hints is skipped."""
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    async with session_maker() as db:
        db.add(CallReview(org_id=org_id, user_email="tim@superdesign.dev", call_id="reviewed",
                          endpoint_id="tavily.web.search", usefulness="useful", invited=invited,
                          client=client, created_at=utcnow_naive() - timedelta(days=days_ago)))
        await db.commit()
    await _routed_call(org_id, "answers invitations", reviewing=False)
    result = await _collect()
    assert result["eligible"] == (1 if compared else 0), result
    assert bool(sent) is compared


async def test_a_busy_team_elsewhere_never_hides_an_eligible_teams_call(clients, search_on,
                                                                       monkeypatch):
    """The scan reads eligible teams' calls only: newer calls from a team that is never compared,
    however many, cannot push an eligible team's marked call out of the per-capability read."""
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    monkeypatch.setattr(compare, "SCAN_PER_CAPABILITY", 2)
    org_id = await _org_id(clients)
    await _routed_call(org_id, "earlier and marked", minutes_ago=50)
    async with session_maker() as db:
        for i in range(5):
            db.add(CallRecord(org_id=org_id + 1000, user_email="pipeline@example.com",
                              tool_name="treg.web.search", method="POST", path="/x", status_code=200,
                              endpoint_id="treg.web.search", provider="treg", credential_tier="routed",
                              call_ref=f"busy-{i}", created_at=utcnow_naive() - timedelta(minutes=i)))
        await db.commit()
    result = await _collect()
    assert (result["read"], result["eligible"]) == (1, 1), result
    assert len(sent) == 1


async def test_every_marker_decision_is_counted_by_reason_and_client(monkeypatch, caplog):
    """The call path says why calls were or were not marked, by client, in one periodic line."""
    import logging

    _compare_on(monkeypatch)
    monkeypatch.setattr(compare, "_mark_counts", {})
    monkeypatch.setattr(compare, "MARK_LOG_EVERY_S", 3600)
    monkeypatch.setattr(hints, "sampled_at", lambda *_: True)
    mcp = [(b"x-treg-client", b"mcp")]
    await compare.mark_eligible("m1", "web.search", mcp, {"identity": {"q": "a"}})
    await compare.mark_eligible("m2", "web.search", mcp + [(b"X-Treg-Route-Max-Cost", b"1")],
                                {"identity": {"q": "b"}})
    await compare.mark_eligible("m3", "web.search", [], {"identity": {"q": "c"}, "cursor": "p2"})
    await compare.mark_eligible("m4", "people.email.find", mcp, {"identity": {"q": "d"}})
    await compare.mark_eligible("m5", "web.search", mcp, {"identity": {"q": "e"}}, archive_opted_out=True)
    assert compare._mark_counts == {"marked/mcp": 1, "route_header/mcp": 1, "page_continuation/-": 1,
                                    "not_paired/mcp": 1, "archive_off/mcp": 1}
    monkeypatch.setattr(compare, "MARK_LOG_EVERY_S", 0)
    with caplog.at_level(logging.INFO, logger=compare.__name__):
        await compare.mark_eligible("m6", "web.search", mcp, {"identity": {"q": "f"}})
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("compare markers"))
    assert '"marked/mcp": 2' in line and "q" not in line.replace("compare markers", "")
    assert compare._mark_counts == {}


async def test_a_rejected_marker_write_is_counted_as_store_failed(monkeypatch):
    """The store answers False when it cannot write; that call is never counted as marked."""
    _compare_on(monkeypatch)
    monkeypatch.setattr(compare, "_mark_counts", {})
    monkeypatch.setattr(compare, "MARK_LOG_EVERY_S", 3600)
    monkeypatch.setattr(hints, "sampled_at", lambda *_: True)

    async def refuse(*_):
        return False

    monkeypatch.setattr(kv.store(), "set_text", refuse)
    await compare.mark_eligible("lost", "web.search", [(b"x-treg-client", b"mcp")],
                                {"identity": {"q": "a"}})
    assert compare._mark_counts == {"store_failed/mcp": 1}


async def test_a_run_names_the_client_of_each_unmarked_call(clients, search_on, monkeypatch):
    _compare_on(monkeypatch)
    _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    await _routed_call(org_id, "never marked", marked=False)
    result = await _collect()
    assert result["skip_reasons"] == {"no_marker": 1}
    assert sum(result["no_marker_by_client"].values()) == 1


async def test_a_page_continuation_gets_no_marker(monkeypatch):
    """A continuation resumes a later page; B would be asked page one, so it is never marked."""
    _compare_on(monkeypatch)
    monkeypatch.setattr(hints, "sampled_at", lambda *_: True)
    await compare.mark_eligible("page-two", "web.search", [],
                                {"identity": {"q": "paged"}, "cursor": "c2"})
    await compare.mark_eligible("page-one", "web.search", [], {"identity": {"q": "paged"}})
    assert await kv.store().get_text(compare.marker_key("page-two")) is None
    assert json.loads(await kv.store().get_text(compare.marker_key("page-one"))) == {"q": "paged"}


async def test_an_archive_opted_out_team_gets_no_marker(clients, search_on, monkeypatch):
    """Its input never sits in the store, even for the marker's two hours."""
    from test_routing import _relay_by_provider

    _compare_on(monkeypatch)
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tavily": [(200, TAVILY_HIT)] * 3, "exa": [(200, EXA_HIT)] * 3}, []))
    org_id = await _org_id(clients)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.archive_opt_out_at = utcnow_naive()
        await db.commit()
    response = await clients.post("/call/treg.web.search", json={"q": "opted out"})
    assert response.status_code == 200, response.text
    assert await kv.store().get_text(compare.marker_key(response.headers["X-Treg-Call-Id"])) is None


async def test_an_input_set_by_expression_is_asked_whole(clients, search_on, monkeypatch):
    """`max_results` comes from `limit` by expression, so A's request cannot be read back; the
    marker carries the input, and B is asked the question the caller sent, `limit` included."""
    _compare_on(monkeypatch)
    sent = _relay_b(monkeypatch)
    org_id = await _org_id(clients)
    await _routed_call(org_id, "with a limit", given={"q": "with a limit", "limit": 5})
    result = await _collect()
    assert (result["called"], result["ready"]) == (1, 1), result
    call, = sent
    assert call["body"]["query"] == "with a limit" and call["body"].get("numResults") == 5, call["body"]


async def test_the_marker_is_off_with_the_feature_and_follows_the_sample(clients, search_on, monkeypatch):
    from test_routing import _relay_by_provider

    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tavily": [(200, TAVILY_HIT)] * 3, "exa": [(200, EXA_HIT)] * 3}, []))
    off = await clients.post("/call/treg.web.search", json={"q": "feature off"})
    assert not await kv.store().has_flag(compare.marker_key(off.headers["X-Treg-Call-Id"]))
    _compare_on(monkeypatch, compare_rate=0.0)
    unsampled = await clients.post("/call/treg.web.search", json={"q": "not sampled"})
    assert not await kv.store().has_flag(compare.marker_key(unsampled.headers["X-Treg-Call-Id"]))


async def test_a_store_fault_on_the_marker_never_touches_the_answer(clients, search_on, monkeypatch):
    from test_routing import _relay_by_provider

    _compare_on(monkeypatch)
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tavily": [(200, TAVILY_HIT)] * 3, "exa": [(200, EXA_HIT)] * 3}, []))

    async def broken(*a, **kw):
        raise RuntimeError("store down")
    monkeypatch.setattr(kv.store(), "set_flag", broken)
    response = await clients.post("/call/treg.web.search", json={"q": "store down"})
    assert response.status_code == 200 and response.json()["output"]["results"]


async def test_a_json_credential_vendor_through_the_real_relay(monkeypatch, fake_getaddrinfo):
    """B's key goes in the JSON body (`location: json`): the real relay injects it from the body
    the worker hands over (`body_read`), and only the network is stubbed. No routed capability
    pairs this vendor today, so B's target and its one request are exercised directly."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ADYNTEL", "PLATFORM-ADYNTEL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "adyntel")
    get_settings.cache_clear()
    try:
        cat = catalog_store.load()
        contract = cat.contracts["meta-ads.library.advertiser"]
        identity, _ = canonical_identity(contract, {"company_domain": "bakery.example"})
        # Its account also needs the email setting: without it B is skipped, never half-sent.
        assert compare._b_target(contract, identity, "adyntel") is None
        monkeypatch.setenv("TREG_PLATFORM_EMAIL_ADYNTEL", "ops@example.com")
        get_settings.cache_clear()
        fake_getaddrinfo({"api.adyntel.com": ["93.184.215.14"]})
        seen = []

        async def vendor(request: httpx.Request) -> httpx.Response:
            seen.append((request, await request.aread()))
            body = json.dumps({"results": [{"page_name": "Bakery", "ad": "Fresh bread"}]}).encode()
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  stream=httpx.ByteStream(body))
        target = compare._b_target(contract, identity, "adyntel")
        assert target is not None and target.ep["id"] == "adyntel.meta-ads.library.advertiser"
        async with httpx.AsyncClient(transport=httpx.MockTransport(vendor)) as client:
            status, raw = await compare._ask_b(target, client, 1)
        assert status == 200
        (request, content), = seen
        assert request.url.host == "api.adyntel.com"
        sent = json.loads(content)
        assert sent["company_domain"] == "bakery.example" and sent["api_key"] == "PLATFORM-ADYNTEL"
        assert sent["email"] == "ops@example.com"
        output = compare._output(target.ep, contract, target.adapter, status, raw, identity)
        assert output["ads"] == [{"page_name": "Bakery", "ad": "Fresh bread"}]
    finally:
        get_settings.cache_clear()


async def test_a_relay_error_fails_one_comparison_and_the_run_goes_on(clients, search_on, monkeypatch):
    _compare_on(monkeypatch)
    calls = []

    async def relay(request, upstream_url, *a, **kw):
        calls.append(upstream_url)
        if len(calls) == 1:
            raise ValueError("credential injection failed")
        raise httpx.ConnectError("down")
    monkeypatch.setattr(compare, "relay", relay)
    org_id = await _org_id(clients)
    await _routed_call(org_id, "first")
    await _routed_call(org_id, "second")
    result = await _collect()
    assert (result["called"], result["failed"]) == (2, 2) and len(calls) == 2



async def test_an_unpaused_team_is_confirmed_only_by_a_fresh_read(monkeypatch):
    from treg.infra import org_flow_control

    assert await org_flow_control.confirmed_unpaused(5)  # flow control off: no pause exists
    monkeypatch.setattr(get_settings(), "org_flow_control_enabled", True)
    answers = iter([None, kv.FlowControlReadFailure.UNAVAILABLE, json.dumps({"org_id": 5, "version": 1, "config": {
        "schema_version": 1, "pause_until": "2000-01-01T00:00:00Z", "rate_limit": None,
        "concurrency_limit": None}})])

    async def read(org):
        return next(answers)
    monkeypatch.setattr(kv.store(), "read_org_flow_control", read)
    assert await org_flow_control.confirmed_unpaused(5)          # no publication
    assert not await org_flow_control.confirmed_unpaused(5)      # unavailable
    assert await org_flow_control.confirmed_unpaused(5)          # an expired pause


async def test_claims_count_on_the_day_they_are_made(clients, monkeypatch):
    org_id = await _org_id(clients)
    days = iter(["20261119", "20261120"])
    monkeypatch.setattr(compare, "_day", lambda: next(days))
    assert await _claim(_claim_values(org_id, "before-midnight"), 1) == "claimed"
    assert await _claim(_claim_values(org_id, "after-midnight"), 1) == "claimed"  # a new day's slot
    async with session_maker() as db:
        from treg.domain.feedback import comparisons
        assert (await comparisons.attempts(db, "20261119"), await comparisons.attempts(db, "20261120")) == (1, 1)


async def test_the_team_cap_is_read_again_inside_the_claim(clients, monkeypatch):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        db.add(CallComparison(org_id=org_id, user_email="x@example.com", call_ref="decided-one",
                              capability="web.search", a_endpoint="exa.web.search", a_provider="exa",
                              b_provider="tavily", status="decided", choice="a"))
        await db.commit()
    results = [await _claim(_claim_values(org_id, ref), 50, team_cap=2) for ref in ("call-a", "call-b")]
    assert results == ["claimed", "capped"]
    assert sorted(r.call_ref for r in await _rows()) == ["call-a", "decided-one"]


async def test_the_last_slot_of_the_day_goes_to_one_claim(clients, monkeypatch):
    import asyncio

    org_id = await _org_id(clients)
    assert await _claim(_claim_values(org_id, "first"), 2) == "claimed"
    results = await asyncio.gather(*(_claim(_claim_values(org_id, ref), 2) for ref in ("second", "third")))
    assert sorted(results) == ["at_limit", "claimed"]
    async with session_maker() as db:
        from treg.domain.feedback import comparisons
        assert await comparisons.attempts(db, compare._day()) == 2
    assert len(await _rows()) == 2


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_a_verdict_on_a_pair_not_shown_is_refused(clients, monkeypatch, status):
    _compare_on(monkeypatch)
    cid = await _seed(await _org_id(clients), first="a", call_ref=f"not-shown-{status}", status=status)
    response = await clients.post("/comparisons", json={"comparison_id": cid, "choice": "1"})
    assert response.status_code == 404
    assert (await _rows())[0].status == status
