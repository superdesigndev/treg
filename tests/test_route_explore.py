"""The exploration split: a salted per-call draw puts one allow-listed provider first on a routed
call, never fails the call, and bills at most what the router's own order would have."""
from __future__ import annotations

import collections
import copy
import json
from types import SimpleNamespace

import pytest

from treg import config
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application.call.settle import DeferredSettle, cap_deferred
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing.plan import explore_draw

from test_call_events import _flushed, events_on  # noqa: F401
from test_marketplace_call import platform_on  # noqa: F401
from test_routing import ROUTED, _relay_by_provider, enrichment_on  # noqa: F401

PERSON = {"full_name": "Jean Dupont", "domain": "acme.fr"}
TOMBA_HIT = (200, {"data": {"email": "j.dupont@acme.fr", "score": 96, "verification": {"status": "valid"}}})
TOMBA_MISS = (200, {"data": {"email": None}})
LEADMAGIC_HIT = (200, {"email": "j.dupont@acme.fr", "status": "valid", "first_name": "Jean",
                        "last_name": "Dupont", "credits_consumed": 1})


# ---- configuration -----------------------------------------------------------------------------

def test_the_split_parses_per_capability():
    raw = json.dumps({"people.email.find": {"percent": 5, "providers": ["hunter", "tomba"]}})
    assert config._route_explore(raw) == {"people.email.find": (5.0, ("hunter", "tomba"))}
    assert config._route_explore("") == {}


@pytest.mark.parametrize("split", [
    [], {"people.email.find": {"percent": 5}}, {"people.email.find": {"percent": 5, "providers": []}},
    {"people.email.find": {"percent": 101, "providers": ["hunter"]}},
    {"people.email.find": {"percent": -1, "providers": ["hunter"]}},
    {"people.email.find": {"percent": True, "providers": ["hunter"]}},
    {"people.email.find": {"percent": 5, "providers": ["Hunter"]}},
    {"people.email.find": {"percent": 5, "providers": ["hunter", "hunter"]}},
    {"people.email.find": {"percent": 5, "providers": ["hunter"], "extra": 1}},
    {"People.Email": {"percent": 5, "providers": ["hunter"]}},
])
def test_a_malformed_split_fails_the_deploy(split):
    with pytest.raises(ValueError):
        config._route_explore(json.dumps(split))


def test_the_split_is_off_by_default():
    assert config.Settings().route_explore_split == {}


# ---- the draw ----------------------------------------------------------------------------------

def test_the_draw_is_per_call_salted_and_uniform():
    providers = ["a", "b", "c", "d"]
    arms = collections.Counter(explore_draw(f"call-{i}", salt="s", percent=5, providers=providers)
                               for i in range(40_000))
    explored = sum(n for arm, n in arms.items() if arm is not None)
    assert 0.045 < explored / 40_000 < 0.055
    for p in providers:
        assert 0.2 < arms[p] / explored < 0.3, arms
    assert explore_draw("call-1", salt="s", percent=5, providers=providers) == \
        explore_draw("call-1", salt="s", percent=5, providers=providers), "a call's arm is stable"
    salted = [explore_draw(f"call-{i}", salt="t", percent=50, providers=providers) for i in range(200)]
    assert salted != [explore_draw(f"call-{i}", salt="s", percent=50, providers=providers) for i in range(200)]
    assert explore_draw("x", salt="s", percent=0, providers=providers) is None
    assert explore_draw("x", salt="s", percent=100, providers=providers) in providers
    assert explore_draw("x", salt="s", percent=100, providers=[]) is None


# ---- the bill ----------------------------------------------------------------------------------

def _held(actual, reserved=50_000):
    return DeferredSettle(call_id="c:r0", billable=True, actual_micro=actual, archive_use=None,
                          reason="", meta={"provider": "p"}, reserved_micro=reserved)


def test_the_explored_hold_is_lowered_to_the_cap_and_never_raised():
    held = _held(20_000)
    assert cap_deferred([held], "c:r0", 4_000) == 4_000
    assert held.actual_micro == 4_000 and held.meta["explore_uncapped_micro"] == 20_000
    cheap = _held(1_000)
    assert cap_deferred([cheap], "c:r0", 4_000) == 1_000 and cheap.actual_micro == 1_000
    whole = _held(None, reserved=20_000)   # no figure: the whole hold would settle
    assert cap_deferred([whole], "c:r0", 4_000) == 4_000 and whole.actual_micro == 4_000
    small = _held(None, reserved=3_000)
    assert cap_deferred([small], "c:r0", 4_000) == 3_000 and small.actual_micro is None
    assert cap_deferred([_held(20_000)], "other", 4_000) is None


# ---- the routed call ---------------------------------------------------------------------------

@pytest.fixture
def explore(monkeypatch, enrichment_on):
    def on(percent=100, providers=("leadmagic",)):
        monkeypatch.setenv("TREG_ROUTE_EXPLORE", json.dumps(
            {"people.email.find": {"percent": percent, "providers": list(providers)}}))
        get_settings.cache_clear()
    return on


async def _plan(header=None, call_ref="c1"):
    options = call_route.RouteOptions.from_headers(lambda key: header.get(key) if header else None)
    caller = SimpleNamespace(org_id=1, org=SimpleNamespace(id=1, platform_overflow_disabled=False))
    return await call_route.build_plan(catalog_store.load().by_id[ROUTED], PERSON, caller, options,
                                       call_ref=call_ref)


async def _call(clients, events_on, relay, answers, headers=None):
    seen: list = []
    relay(_relay_by_provider(answers, seen))
    r = await clients.post(f"/call/{ROUTED}", json=PERSON, headers=headers or {})
    events = await _flushed(events_on)
    ref = r.headers.get("x-treg-call-id")
    meta = next((e for e in events if e["call_ref"] == ref), {}).get("input_meta") or {}
    return r, [p for p, *_ in seen], meta


@pytest.fixture
def relay(monkeypatch):
    return lambda fake: monkeypatch.setattr(call_service, "relay", fake)


async def test_an_explored_call_asks_the_drawn_provider_first_and_keeps_the_rest(clients, explore):
    default = await _plan()
    assert default.explore is None and "route_explore" not in (default.route_rule or {})
    explore()
    plan = await _plan()
    first = plan.candidates[0]
    assert first.endpoint["provider"] == "leadmagic" and plan.explore.provider == "leadmagic"
    rest = [c.endpoint["id"] for c in default.candidates if c.endpoint["provider"] != "leadmagic"]
    assert [c.endpoint["id"] for c in plan.candidates[1:]] == rest, "the router's order follows"
    assert plan.route_rule["route_explore"] == "leadmagic"
    assert plan.explore.cap_micro == min(c.price_micro for c in default.candidates)
    assert first.price_micro > plan.explore.cap_micro, "leadmagic costs more than the default's cheapest"


async def test_a_preview_or_a_callers_own_order_never_explores(clients, explore):
    explore()
    assert (await _plan(call_ref=None)).explore is None
    pinned = await _plan({call_route.PREFER_HEADER: "tomba"})
    assert pinned.explore is None and "route_explore" not in pinned.route_rule
    off = await _plan({call_route.WATERFALL_HEADER: "0"})
    assert off.explore is None and "route_explore" not in off.route_rule


async def test_a_control_call_records_the_default_arm(clients, explore):
    explore(percent=0)
    plan = await _plan()
    assert plan.explore is None and plan.route_rule["route_explore"] == "default"


async def test_drawing_the_routers_own_first_pick_moves_and_caps_nothing(clients, explore):
    explore(providers=("tomba",))
    plan = await _plan()
    assert plan.explore is None and plan.route_rule["route_explore"] == "tomba"
    assert plan.candidates[0].endpoint["provider"] == "tomba"


async def test_a_provider_not_in_the_plan_is_never_drawn(clients, explore):
    explore(providers=("no-such-provider",))
    plan = await _plan()
    assert plan.explore is None and "route_explore" not in plan.route_rule


async def test_an_explored_hit_bills_at_most_the_default_orders_cheapest_hit(
        clients, explore, events_on, relay):
    from test_call_vendor_path import _balance
    explore()
    cap = (await _plan()).explore.cap_micro
    before = await _balance(clients)
    r, seen, meta = await _call(clients, events_on, relay, {
        "leadmagic": [LEADMAGIC_HIT], "*": [TOMBA_HIT]})
    assert r.status_code == 200, r.text
    assert seen == ["leadmagic"], r.json()["_treg"]
    charged = int(r.headers["x-treg-cost-micro"])
    assert charged == cap == r.json()["_treg"]["tried"][0]["charged_micro"]
    assert before - await _balance(clients) == charged, "the ledger settled the capped amount"
    assert meta["route_explore"] == "leadmagic" and meta["route_order"] == "explore"
    assert int(meta["route_explore_absorbed_micro"]) > 0


async def test_an_explored_miss_is_free_and_falls_through(clients, explore, events_on, relay):
    explore()
    r, seen, meta = await _call(clients, events_on, relay, {
        "leadmagic": [(200, {"email": None, "message": "Email not found"})], "tomba": [TOMBA_HIT]})
    assert r.status_code == 200, r.text
    tried = r.json()["_treg"]["tried"]
    assert seen == ["leadmagic", "tomba"] and r.json()["_treg"]["served_by"].startswith("tomba")
    assert tried[0]["charged_micro"] == 0
    assert int(r.headers["x-treg-cost-micro"]) == tried[1]["charged_micro"]
    assert meta["route_order"] == "explore" and meta["route_explore"] == "leadmagic"


@pytest.mark.parametrize("failure", [(400, {"error": "bad request"}), (500, {"error": "boom"}),
                                     (401, {"error": "invalid key"})])
async def test_an_explored_failure_never_fails_the_call(clients, explore, events_on, relay, failure):
    explore()
    r, seen, _ = await _call(clients, events_on, relay, {"leadmagic": [failure], "tomba": [TOMBA_HIT]})
    assert r.status_code == 200, r.text
    assert seen[:2] == ["leadmagic", "tomba"] and r.json()["_treg"]["outcome"] == "hit"
    assert r.json()["_treg"]["tried"][0]["charged_micro"] == 0


async def test_an_explored_failure_leaves_the_routers_own_miss_unmarked(clients, explore, events_on, relay):
    explore()
    r, _, _ = await _call(clients, events_on, relay, {"leadmagic": [(500, {"error": "boom"})],
                                               "*": [copy.deepcopy(TOMBA_MISS) for _ in range(6)]})
    assert r.status_code == 200, r.text
    body = r.json()["_treg"]
    assert body["outcome"] == "miss" and "incomplete" not in body, \
        "only the explored provider failed; the router's own walk was a clean miss"


async def test_an_explored_provider_refused_before_sending_is_skipped(
        clients, explore, events_on, relay, monkeypatch):
    from treg.application.call import authorize
    from treg.domain.governance import access
    real = access.enforce_deny

    async def deny_leadmagic(caller, url, *args, **kwargs):
        if "leadmagic" in url:
            raise access.AccessPolicyError("blocked by this team's policy")
        return await real(caller, url, *args, **kwargs)
    monkeypatch.setattr(authorize.access_policy, "enforce_deny", deny_leadmagic)
    explore()
    r, seen, meta = await _call(clients, events_on, relay, {"tomba": [TOMBA_HIT]})
    assert r.status_code == 200, r.text
    assert seen == ["tomba"] and r.json()["_treg"]["tried"][0]["outcome"] == "skipped"
    assert meta["route_explore"] == "leadmagic" and meta["route_order"] == "default"


def test_attribution_names_the_exploration_order():
    parent = SimpleNamespace(route_summary={"route_rule": {"route_explore": "leadmagic"}})
    plan = SimpleNamespace(orders={"default": ["tomba", "hunter"], "explore": ["leadmagic", "tomba", "hunter"]})
    tried = []
    for endpoint in ("leadmagic", "tomba"):
        call_route._note_call(parent, plan, SimpleNamespace(endpoint={"id": endpoint}),
                              SimpleNamespace(reached_provider=True), tried)
        tried.append(call_route.Attempt(endpoint, endpoint, "miss", None, 0))
    assert parent.route_summary["route_rule"] == {"route_explore": "leadmagic", "route_order": "explore"}
