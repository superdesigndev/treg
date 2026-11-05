"""Published route rules: parsing, matching, the worker's snapshot, and the routed plan."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from treg.application import call_events as ev
from treg.application.call import route_rules as reader_app
from treg.application.call import service as call_service
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import rules as R
from treg.infra import kv

from test_call_events import _flushed, events_on  # noqa: F401
from test_marketplace_call import platform_on  # noqa: F401
from test_routing import ROUTED, _first_providers, _relay_by_provider, enrichment_on  # noqa: F401


def envelope(version=1, rules=None, enabled=True, **config):
    return json.dumps({"version": version, "config": {
        "schema_version": 1, "enabled": enabled,
        "rules": rules if rules is not None else [
            {"id": "find-fr", "capability": "people.email.find", "match": {"tld": "fr"},
             "order": ["tomba"]}],
        **config}})


# ---- parsing and matching ----------------------------------------------------------------------

def test_a_rule_set_parses_into_rules_and_a_canonical_form():
    parsed = R.parse(envelope(version=3))
    assert parsed.version == 3 and parsed.enabled
    assert parsed.rules == (R.Rule("find-fr", "people.email.find", (("tld", "fr"),), ("tomba",)),)
    assert parsed.canonical == R.parse(envelope(version=4)).canonical


def _rule(**overrides):
    return {"id": "r", "capability": "people.email.find", "match": {"tld": "fr"},
            "order": ["tomba"], **overrides}


@pytest.mark.parametrize("raw", [
    "{", " " * (R.MAX_BYTES + 1), '{"version":1,"version":1,"config":{}}',
    json.dumps({"version": 1}), envelope(version=0), envelope(version=True),
    envelope(version=2147483648), envelope(schema_version=2), envelope(enabled="yes"),
    envelope(extra=True), envelope(rules={}),
    envelope(rules=[_rule(), _rule()]),                    # duplicate id
    envelope(rules=[_rule(id="Upper")]), envelope(rules=[_rule(extra=1)]),
    envelope(rules=[{"id": "r", "capability": "people.email.find", "order": ["tomba"]}]),
    envelope(rules=[_rule(match={})]), envelope(rules=[_rule(match={"tld": ""})]),
    envelope(rules=[_rule(match={"tld": 1})]), envelope(rules=[_rule(match={"TLD": "fr"})]),
    envelope(rules=[_rule(order=[])]), envelope(rules=[_rule(order=["tomba", "tomba"])]),
    envelope(rules=[_rule(order=["Tomba"])]), envelope(rules=[_rule(order="tomba")]),
    envelope(rules=[_rule(reason="")]),
    envelope(rules=[_rule(id=f"r{i}") for i in range(R.MAX_RULES_PER_CAPABILITY + 1)]),
])
def test_a_malformed_rule_set_is_refused(raw):
    with pytest.raises((ValueError, TypeError)):
        R.parse(raw)


def test_a_rule_must_be_able_to_take_effect():
    contracts = catalog_store.load().contracts
    check = lambda rule: R.check_against(  # noqa: E731
        R.parse(envelope(rules=[rule])), contracts, reader_app._servers_of, ev.segment_checks)
    check(_rule())
    # a call recorded as name + domain may also carry a LinkedIn URL, which this finder takes
    check(_rule(match={"input_type": "domain+full_name"}, order=["aviato"]))
    check(_rule(match={"tld": "中国"}))
    for rule, why in (
            (_rule(capability="no.such"), "no contract"),
            (_rule(match={"seniority": "vp"}), "records no seniority"),
            # a provider with an email finder in the catalog but no routed child for the capability
            (_rule(order=["tomba", "anyapi"]), "anyapi cannot serve routed"),
            # a capability with no routed endpoint
            (_rule(capability="people.phone.verify", match={"input_type": "phone"}, order=["twilio"]),
             "is not routed"),
            # a web search never records a domain segment
            (_rule(capability="web.search", order=["exa"]), "records no tld"),
            # a variant spelled in another order is never recorded
            (_rule(match={"input_type": "full_name+domain"}), "never records input_type=full_name"),
            (_rule(match={"domain_class": "startup"}), "never records domain_class"),
            # `{q}` comes first, so a call carrying both is always recorded as `q`
            (_rule(capability="google.serp.organic", match={"input_type": "ai_overview+q"}, order=["x"]),
             "never records input_type=ai_overview"),
            (_rule(match={"tld": "FR"}), "never records tld"),
            # a finder that takes a name and a domain cannot answer a LinkedIn-only call
            (_rule(match={"input_type": "linkedin_url"}, order=["hunter"]), "hunter cannot serve .* for linkedin_url"),
    ):
        with pytest.raises(ValueError, match=why):
            check(rule)


def test_segment_checks_are_what_the_event_can_record():
    contracts = catalog_store.load().contracts
    find = ev.segment_checks(contracts["people.email.find"])
    assert {"input_type", "tld", "domain_class"} <= set(find)
    assert find["input_type"]("domain+full_name") and not find["input_type"]("full_name+domain")
    assert find["tld"]("fr") and find["tld"]("中国") and not find["tld"]("FR") and not find["tld"](".fr")
    assert ev.segment(contracts["people.email.find"], {"full_name": "A B", "domain": "example.中国"})[1]["tld"] == "中国"
    assert set(ev.segment_checks(contracts["people.phone.verify"])) == {"input_type"}


def test_the_most_specific_rule_wins_then_the_first_listed():
    parsed = R.parse(envelope(rules=[
        _rule(id="fr", order=["hunter"]), _rule(id="fr-again", order=["tomba"]),
        _rule(id="fr-named", match={"tld": "fr", "input_type": "domain+full_name"}, order=["enrow"]),
    ]))
    find = "people.email.find"
    assert R.match(parsed, find, {"tld": "fr", "input_type": "domain+full_name"}).id == "fr-named"
    assert R.match(parsed, find, {"tld": "fr", "input_type": "linkedin_url"}).id == "fr"
    assert R.match(parsed, find, {"tld": "de"}) is None
    assert R.match(parsed, "people.phone.find", {"tld": "fr"}) is None
    assert R.match(R.parse(envelope(enabled=False)), find, {"tld": "fr"}) is None
    assert R.match(None, find, {"tld": "fr"}) is None


# ---- the worker's snapshot ---------------------------------------------------------------------

@pytest.fixture
def snapshot_env(monkeypatch):
    clock = [100.0]
    store = SimpleNamespace(read_route_rules=AsyncMock(return_value=None))
    monkeypatch.setattr(reader_app, "_clock", lambda: clock[0])
    monkeypatch.setattr(reader_app, "_store", lambda: store)
    monkeypatch.setattr(reader_app, "_servers_of", lambda cap, variant: {"tomba", "hunter", "enrow"})
    return reader_app.Reader(), store, clock


async def _read(reader, refresh_s=30):
    """What a plan sees once the refresh it started has landed."""
    reader.current(refresh_s)
    if reader.task is not None:
        await reader.task
    return reader.current(refresh_s)[1]


async def test_one_read_per_refresh_interval_and_a_missing_key_means_no_rules(snapshot_env):
    reader, store, clock = snapshot_env
    assert reader.current(30) == (False, None), "a cold worker plans in the YAML order"
    await reader.task
    assert reader.current(30) == (True, None), "read: none published"
    store.read_route_rules.return_value = envelope()
    assert await _read(reader) is None, "the next read waits for the interval"
    clock[0] += 30
    assert (await _read(reader)).version == 1
    assert store.read_route_rules.await_count == 2
    store.read_route_rules.return_value = None
    clock[0] += 30
    assert await _read(reader) is None


async def test_a_plan_never_waits_for_a_slow_read(snapshot_env):
    reader, store, clock = snapshot_env
    store.read_route_rules.return_value = envelope()
    assert (await _read(reader)).version == 1
    gate = asyncio.Event()

    async def hangs():
        await gate.wait()
    store.read_route_rules.side_effect = hangs
    clock[0] += 30
    seen = [reader.current(30) for _ in range(8)]
    assert all(observed and snap.version == 1 for observed, snap in seen)
    await asyncio.sleep(0)
    assert store.read_route_rules.await_count == 2, "concurrent plans share one refresh"
    gate.set()
    await reader.task


async def test_an_unchanged_value_is_not_checked_again(snapshot_env, monkeypatch):
    reader, store, clock = snapshot_env
    store.read_route_rules.return_value = envelope()
    checks = []
    original = R.check_against
    monkeypatch.setattr(R, "check_against", lambda *a: checks.append(1) or original(*a))
    for _ in range(3):
        await _read(reader)
        clock[0] += 30
    assert len(checks) == 1


@pytest.mark.parametrize("bad", [
    kv.FlowControlReadFailure.UNAVAILABLE, kv.FlowControlReadFailure.INVALID, "{",
    envelope(version=2, rules=[_rule(order=["nobody"])]),       # names a provider that serves nothing
    envelope(version=1, rules=[_rule(order=["hunter"])]),       # different content, same version
    envelope(version=0),
])
async def test_every_failure_keeps_the_last_valid_rule_set(snapshot_env, bad):
    reader, store, clock = snapshot_env
    store.read_route_rules.return_value = envelope(version=1)
    assert (await _read(reader)).version == 1
    store.read_route_rules.return_value = bad
    clock[0] += 30
    kept = await _read(reader)
    assert kept.version == 1 and kept.rules[0].order == ("tomba",)


async def test_a_read_that_raises_keeps_the_rules_and_a_higher_version_replaces_them(snapshot_env):
    reader, store, clock = snapshot_env
    store.read_route_rules.return_value = envelope(version=1)
    await _read(reader)
    store.read_route_rules.side_effect = RuntimeError("boom")
    clock[0] += 30
    assert (await _read(reader)).version == 1
    store.read_route_rules.side_effect = None
    store.read_route_rules.return_value = envelope(version=2, rules=[_rule(order=["hunter"])])
    clock[0] += 30
    assert (await _read(reader)).rules[0].order == ("hunter",)


# ---- the routed plan and its event -------------------------------------------------------------

@pytest.fixture
async def published(monkeypatch):
    """Route rules on, with the published set served from a fake store and already read."""
    reader_app.reset()
    store = SimpleNamespace(read_route_rules=AsyncMock(return_value=envelope()))
    monkeypatch.setattr(reader_app, "_store", lambda: store)
    monkeypatch.setattr(reader_app, "get_settings", lambda: SimpleNamespace(
        route_rules_enabled=True, route_rules_refresh_s=30))

    async def publish(raw):
        store.read_route_rules.return_value = raw
        reader_app._reader.next_refresh = 0.0
        await _read(reader_app._reader)
    await publish(envelope())
    yield publish
    reader_app.reset()


async def test_a_published_rule_goes_ahead_of_the_yaml_order(clients, enrichment_on, monkeypatch, published):
    cat = catalog_store.load()
    monkeypatch.setitem(cat.contracts, "people.email.find", dataclasses.replace(
        cat.contracts["people.email.find"], prefer_by_domain_suffix={"fr": ("hunter",)}))
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    assert await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.fr"}) == ["leadmagic", "hunter"]
    assert await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.de"}, n=1) != ["leadmagic"]
    header = await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.fr"}, header="hunter", n=1)
    assert header == ["hunter"], "the caller's prefer header replaces published rules"


async def test_off_by_default_the_published_set_is_never_read(clients, enrichment_on, monkeypatch):
    from treg.application.call import route as call_route
    reader_app.reset()
    store = SimpleNamespace(read_route_rules=AsyncMock(return_value=envelope()))
    monkeypatch.setattr(reader_app, "_store", lambda: store)
    monkeypatch.setattr(call_route.call_events, "segment", lambda *a: pytest.fail("segmented while off"))
    await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert store.read_route_rules.await_count == 0 and reader_app._reader.task is None


async def _parent_meta(clients, events_on, body, headers=None):
    routed = await clients.post(f"/call/{ROUTED}", json=body, headers=headers or {})
    assert routed.status_code == 200, routed.text
    events = await _flushed(events_on)
    ref = routed.headers["x-treg-call-id"]
    children = [e for e in events if e.get("parent_ref") == ref]
    assert children and not any("route_order" in (c.get("input_meta") or {}) for c in children), \
        "children join by parent_ref"
    return next(e for e in events if e["call_ref"] == ref)["input_meta"]


TOMBA_HIT = {"tomba": [(200, {"data": {"email": "j.dupont@acme.fr", "score": 96,
                                       "verification": {"status": "valid"}}})]}


async def test_the_routed_event_records_the_version_the_rule_and_what_ordered_it(
        clients, enrichment_on, events_on, monkeypatch, published):
    from test_routing import _example
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"leadmagic": [(200, _example("leadmagic.people.email.find"))], "*": [(200, {})] * 5}, seen))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert seen[0][0] == "leadmagic"
    assert meta == {"domain_class": "company", "tld": "fr", "route_order": "rule",
                    "route_rules_version": "2", "route_rule": "r"}


async def test_a_rule_that_names_the_default_first_did_not_order_the_call(
        clients, enrichment_on, events_on, monkeypatch, published):
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), []))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert meta["route_order"] == "default" and meta["route_rule"] == "find-fr"


async def test_a_segment_no_rule_matches_still_records_the_version(
        clients, enrichment_on, events_on, monkeypatch, published):
    await published(envelope(version=8, enabled=False))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), []))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert meta["route_order"] == "default" and meta["route_rules_version"] == "8"
    assert "route_rule" not in meta


async def test_a_rule_whose_provider_the_caller_excluded_did_not_order_the_call(
        clients, enrichment_on, events_on, monkeypatch, published):
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), []))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"},
                              {"X-Treg-Route-Exclude": "leadmagic"})
    assert meta["route_order"] == "default" and meta["route_rule"] == "r"


async def test_a_rule_pick_skipped_by_the_cost_ceiling_did_not_order_the_call(
        clients, enrichment_on, events_on, monkeypatch, published):
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), seen))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"},
                              {"X-Treg-Route-Max-Cost": "0.019"})
    assert [p for p, *_ in seen] == ["tomba"], "leadmagic is over the ceiling and never called"
    assert meta["route_order"] == "default" and meta["route_rule"] == "r"


def _attributed(orders, calls):
    """Run the calls ((endpoint, called?) in attempt order) through the attribution."""
    from treg.application.call import route as call_route
    parent = SimpleNamespace(route_summary={"route_rule": {"route_rule": "r"}})
    plan = SimpleNamespace(orders=orders)
    tried = []
    for endpoint, called in calls:
        call_route._note_call(parent, plan, SimpleNamespace(endpoint={"id": endpoint}),
                              SimpleNamespace(reached_provider=called), tried)
        tried.append(call_route.Attempt(endpoint, endpoint, "miss" if called else "skipped", None, 0))
    return parent.route_summary["route_rule"]


DEFAULT = ["tomba", "findymail", "hunter"]


@pytest.mark.parametrize("orders, calls, want", [
    # the rule's pick refused locally (policy, allowance, ceiling): the default first was called
    ({"default": DEFAULT, "rule": ["leadmagic", *DEFAULT]},
     [("leadmagic", False), ("tomba", True), ("findymail", True)], {"route_order": "default"}),
    # the same retries with the rule on as off: nothing to attribute to it
    ({"default": DEFAULT, "rule": ["tomba", "hunter", "findymail"]},
     [("tomba", True), ("findymail", True)], {"route_order": "default"}),
    ({"default": DEFAULT}, [("tomba", True), ("findymail", True)], {"route_order": "default"}),
    # the rule moved the first call, then a retry
    ({"default": DEFAULT, "rule": ["leadmagic", "hunter", "tomba", "findymail"]},
     [("leadmagic", True), ("hunter", True)], {"route_order": "rule", "route_retry_order": "rule"}),
    # neither order would have made this retry next: quality reordered it
    ({"default": DEFAULT}, [("tomba", True), ("hunter", True)],
     {"route_order": "default", "route_retry_order": "quality"}),
    ({"default": DEFAULT}, [], {}),
    ({"default": DEFAULT}, [("tomba", False), ("findymail", False)], {}),   # nothing reached
    ({}, [("hunter", False), ("tomba", True)], {"route_order": "header"}),
    ({}, [("hunter", False)], {}),                                          # header, nothing reached
])
def test_attribution_follows_the_providers_actually_called(orders, calls, want):
    assert _attributed(orders, calls) == {"route_rule": "r", **want}


async def test_a_rule_pick_refused_by_the_teams_policy_did_not_order_the_call(
        clients, enrichment_on, events_on, monkeypatch, published):
    from treg.application.call import authorize
    from treg.domain.governance import access

    real = access.enforce_deny

    async def deny_leadmagic(caller, url, *args, **kwargs):
        if "leadmagic" in url:
            raise access.AccessPolicyError("blocked by this team's policy")
        return await real(caller, url, *args, **kwargs)
    monkeypatch.setattr(authorize.access_policy, "enforce_deny", deny_leadmagic)
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), seen))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert [p for p, *_ in seen] == ["tomba"], "leadmagic is refused here and never called"
    assert meta["route_order"] == "default" and "route_retry_order" not in meta


@pytest.fixture
def busy_reserve(monkeypatch):
    """Money admission times out for the first `n[0]` reserves, before anything leaves."""
    from contextlib import asynccontextmanager

    from treg.infra import money_admission
    n, real = [0], money_admission.admit

    @asynccontextmanager
    async def admit(org_ids, *, operation, **kwargs):
        if operation == "reserve" and n[0] > 0:
            n[0] -= 1
            raise money_admission.ReserveAdmissionTimeout()
        async with real(org_ids, operation=operation, **kwargs):
            yield
    monkeypatch.setattr(money_admission, "admit", admit)
    return n


async def test_a_rule_pick_refused_by_money_admission_did_not_order_the_call(
        clients, enrichment_on, events_on, monkeypatch, published, busy_reserve):
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    busy_reserve[0] = 1
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), seen))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert [p for p, *_ in seen] == ["tomba"]
    assert meta["route_order"] == "default"


async def test_no_provider_reached_records_no_order(clients, enrichment_on, events_on, monkeypatch,
                                                     published, busy_reserve):
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    busy_reserve[0] = 99
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), seen))
    routed = await clients.post(f"/call/{ROUTED}", json={"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert seen == [] and routed.status_code >= 400
    over = await clients.post(f"/call/{ROUTED}", json={"full_name": "Jean Dupont", "domain": "acme.fr"},
                              headers={"X-Treg-Route-Prefer": "leadmagic", "X-Treg-Route-Max-Cost": "0.000001"})
    assert seen == [] and over.status_code >= 400
    events = await _flushed(events_on)
    for r in (routed, over):
        meta = next(e for e in events if e["call_ref"] == r.headers["x-treg-call-id"])["input_meta"]
        assert "route_order" not in meta and meta["route_rule"] == "r"


def _relay_blocking(blocked: set[str], answers: dict, seen: list):
    """The scripted relay, refusing `blocked` providers locally as the real relay's address check
    does: before the send, so the provider never sees the request."""
    from treg.application.call.types import GatewayFailed
    inner = _relay_by_provider(answers, seen)

    async def _relay(request, upstream_url, *args, **kwargs):
        if any(b in upstream_url for b in blocked):
            raise GatewayFailed("ssrf_refused", status_code=502, detail="non-public address")
        return await inner(request, upstream_url, *args, **kwargs)
    return _relay


@pytest.mark.parametrize("blocked, leadmagic, order", [
    ({"leadmagic"}, [], "default"),                                   # refused here: never reached
    (set(), [(503, {"error": "unavailable"})], "rule"),               # reached, failed upstream
])
async def test_a_provider_refused_before_the_send_is_not_reached(
        clients, enrichment_on, events_on, monkeypatch, published, blocked, leadmagic, order):
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_blocking(
        blocked, {**copy.deepcopy(TOMBA_HIT), "leadmagic": leadmagic}, seen))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert [p for p, *_ in seen] == ([] if blocked else ["leadmagic"]) + ["tomba"]
    assert meta["route_order"] == order


async def test_every_provider_refused_before_the_send_records_no_order(
        clients, enrichment_on, events_on, monkeypatch, published):
    await published(envelope(version=2, rules=[_rule(order=["leadmagic"])]))
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_blocking({"://"}, {}, seen))
    routed = await clients.post(f"/call/{ROUTED}", json={"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert seen == [] and routed.status_code >= 400
    events = await _flushed(events_on)
    meta = next(e for e in events if e["call_ref"] == routed.headers["x-treg-call-id"])["input_meta"]
    assert "route_order" not in meta and meta["route_rule"] == "r"


async def test_a_caller_header_is_recorded_as_what_ordered_the_call(
        clients, enrichment_on, events_on, monkeypatch, published):
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(copy.deepcopy(TOMBA_HIT), []))
    meta = await _parent_meta(clients, events_on, {"full_name": "Jean Dupont", "domain": "acme.fr"},
                              {"X-Treg-Route-Prefer": "tomba"})
    assert meta["route_order"] == "header" and meta["route_rule"] == "find-fr"


@pytest.mark.parametrize("rated_on, first, provider, order", [
    ("linkedin_url", "blitzapi", "harvest", "quality"),   # quality moved the rule's pick down
    ("email", "harvestapi", "harvest", "rule"),           # no rating for this variant: the rule decided
])
async def test_what_ordered_the_first_attempt_is_what_changed_it(
        clients, platform_on, events_on, monkeypatch, published, rated_on, first, provider, order):
    from test_route_quality_upgrade import LINKEDIN, _Reader, _rated
    from test_routing import _example
    from treg.application.call import route as call_route
    from treg.config import get_settings

    for p in ("BLITZAPI", "HARVESTAPI"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "blitzapi,harvestapi")
    get_settings.cache_clear()
    await published(envelope(version=2, rules=[_rule(id="enrich-li", capability="people.enrich",
                                                     match={"input_type": "linkedin_url"}, order=[first])]))
    seen = []
    snapshot = _rated({"harvestapi.linkedin.user.profile": (0.78, 0.99),
                       "blitzapi.people.enrich": (0.8, 0.8)}, rated_on)
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(snapshot))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"harvest": [(200, _example("harvestapi.linkedin.user.profile"))]}, seen))
    routed = await clients.post("/call/treg.people.enrich", json=LINKEDIN, headers={"X-Treg-Route-Quality": "1"})
    assert routed.status_code == 200, routed.text
    assert [s[0] for s in seen] == [provider]
    events = await _flushed(events_on)
    meta = next(e for e in events if e["call_ref"] == routed.headers["x-treg-call-id"])["input_meta"]
    assert meta["route_order"] == order and meta["route_rule"] == "enrich-li"
    get_settings.cache_clear()


async def test_a_decisive_reordering_of_the_providers_left_is_recorded(
        clients, platform_on, events_on, monkeypatch, published):
    from test_route_decisive import ADDRESS, ON, VERIFY, _Reader, _quality, _word
    from test_route_verify import _bb, _relay
    from treg.application.call import route as call_route
    from treg.config import get_settings

    for p in ("BOUNCEBAN", "TOMBA", "HUNTER"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "bounceban,tomba,hunter")
    get_settings.cache_clear()
    await published(envelope(version=2, rules=[_rule(id="verify-co", capability="people.email.verify",
                                                     match={"domain_class": "company"}, order=["bounceban"])]))
    retry = []
    for best, other in (("hunter", "tomba"), ("tomba", "hunter")):
        seen = []
        monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(_quality(
            {f"{best}.people.email.verify": (0.95, 0.99), f"{other}.people.email.verify": (0.6, 0.9)})))
        monkeypatch.setattr(call_service, "relay", _relay(
            {"bounceban": [_bb("unknown")], f"{best}-verify": [_word("invalid")],
             f"{other}-verify": [_word("valid")]}, seen))
        events_on.clear()
        r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
        assert r.status_code == 200, r.text
        assert seen == ["bounceban", f"{best}-verify"]
        meta = next(e for e in await _flushed(events_on)
                    if e["call_ref"] == r.headers["x-treg-call-id"])["input_meta"]
        assert meta["route_rule"] == "verify-co"
        retry.append(meta.get("route_retry_order"))
    # One of the two ratings matches the price order of the providers left, the other changes it.
    assert sorted(retry, key=str) == sorted(["quality", None], key=str)
    get_settings.cache_clear()


# ---- storage -----------------------------------------------------------------------------------

@pytest.fixture(params=["model", "migration"])
async def rule_table(request):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from treg.infra import db
    from treg.models import RouteRuleVersion

    table = RouteRuleVersion.__table__
    migration = import_module("treg.alembic.versions.0096_route_rule_version")
    await db.reset_db()
    async with db._engine.connect() as connection:
        transaction = await connection.begin()
        try:
            if request.param == "migration":
                await connection.run_sync(table.drop)

                def upgrade(sync):
                    with Operations.context(MigrationContext.configure(sync)):
                        migration.upgrade()
                await connection.run_sync(upgrade)
            yield connection, table
        finally:
            await transaction.rollback()


async def test_only_an_approved_version_can_be_published(rule_table):
    from datetime import datetime

    import sqlalchemy as sa

    connection, table = rule_table
    now = datetime(2026, 1, 1)
    row = {"version": 1, "config": json.loads(envelope())["config"], "reason": "synthetic",
           "created_by": "agent", "created_at": now}
    await connection.execute(table.insert().values(**row))
    for bad in ({"publish_status": "pending"},                          # not approved
                {"review_status": "approved"},                         # no reviewer
                {"review_status": "approved", "reviewed_by": "op", "reviewed_at": now,
                 "publish_status": "published"}):                      # no publisher
        with pytest.raises(sa.exc.IntegrityError):
            async with connection.begin_nested():
                await connection.execute(table.update().where(table.c.version == 1).values(**bad))
    await connection.execute(table.update().where(table.c.version == 1).values(
        review_status="approved", reviewed_by="op", reviewed_at=now,
        publish_status="published", published_by="agent", published_at=now))
    got = (await connection.execute(sa.select(table.c.config, table.c.publish_status))).one()
    assert got.publish_status == "published" and got.config["rules"][0]["id"] == "find-fr"
