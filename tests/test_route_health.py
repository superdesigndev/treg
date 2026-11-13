"""Routed child guards: short-window health, the per-child time bound, and the incomplete miss."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlmodel import select

from treg.application.call import route_health
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog.routing import health as rules
from treg.domain.catalog.routing.plan import Candidate, rank
from treg.infra import kv
from treg.infra.db import session_maker
from treg.infra.upstream import relay as relay_module
from treg.models import Hold

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _relay_by_provider, enrichment_on  # noqa: F401

ROUTED = "treg.people.email.verify"
LEADMAGIC = "leadmagic.people.email.verify"
TOMBA = "tomba.people.email.verify"
TOMBA_MISS = (200, {"data": {"email": {"status": None, "score": None}}})
HEADERS = {"X-Treg-Route-Prefer": "leadmagic,tomba", "X-Treg-Route-Exclude": "hunter"}


# ---- pure ------------------------------------------------------------------------------------

def test_verdict_needs_enough_calls_then_reads_failures_before_slowness():
    t = rules.Thresholds(min_calls=4, fail_pct=50, slow_pct=50)
    assert rules.verdict(rules.Tally(3, 3, 0), t) == rules.OK          # too few to judge
    assert rules.verdict(rules.Tally(4, 2, 0), t) == rules.FAILING
    assert rules.verdict(rules.Tally(4, 1, 2), t) == rules.SLOW
    assert rules.verdict(rules.Tally(4, 1, 1), t) == rules.OK


def test_failed_status_is_a_provider_fault_never_a_capacity_or_caller_answer():
    assert all(rules.failed_status(s) for s in (500, 502, 503, 504, 405, 408, 410))
    assert not any(rules.failed_status(s) for s in (None, 200, 400, 401, 404, 402, 429))


def test_child_timeout_is_the_floor_or_twice_p95_never_past_the_relay():
    assert rules.child_timeout_s(30, 180, None) == 30
    assert rules.child_timeout_s(30, 180, 10_000) == 30
    assert rules.child_timeout_s(30, 180, 40_000) == 80
    assert rules.child_timeout_s(30, 180, 400_000) == 180
    assert rules.child_timeout_s(0, 180, 40_000) is None


def _cand(provider: str, tier: str = "platform", health: str = "ok", price: int = 1000) -> Candidate:
    return Candidate(endpoint={"id": f"{provider}.x", "provider": provider, "cost": {"type": "per_call"}},
                     adapter=None, variant=("email",), tier=tier, price_micro=price, hit_rate=0.5,
                     ok_rate=None, p50_ms=None, last_ok_days=None, health=health)


def test_rank_asks_slow_and_failing_children_after_healthy_ones_of_their_tier():
    ranked = rank([_cand("a", health="failing", price=1), _cand("b", health="slow", price=2),
                   _cand("c", price=3), _cand("own", tier="credential", price=0)])
    assert [c.endpoint["provider"] for c in ranked] == ["own", "c", "b", "a"]
    # the caller's own prefer order still stands
    ranked = rank([_cand("a", health="slow"), _cand("c")], prefer=["a"], caller_prefer=True)
    assert [c.endpoint["provider"] for c in ranked] == ["a", "c"]


async def test_local_store_sums_the_window_and_forgets_older_buckets():
    store = kv.LocalStore()
    await store.note_route_health("e", 10, failed=True, slow=False, ttl_s=60)
    await store.note_route_health("e", 11, failed=False, slow=True, ttl_s=60)
    await store.note_route_health("e", 5, failed=True, slow=False, ttl_s=60)
    assert (await store.read_route_health(["e", "f"], [10, 11])) == {"e": (2, 1, 1), "f": (0, 0, 0)}


async def test_a_childs_failures_age_out_of_the_window(monkeypatch):
    for _ in range(3):
        route_health.note(LEADMAGIC, failed=True, slow=False)
    await asyncio.sleep(0)   # the background store writes
    route_health.reset()
    assert (await route_health.read([LEADMAGIC]))[LEADMAGIC].failed == 3
    later = time.time() + 2 * get_settings().route_health_window_s
    monkeypatch.setattr(route_health.time, "time", lambda: later)
    route_health.reset()
    assert (await route_health.read([LEADMAGIC]))[LEADMAGIC] == rules.Tally()


# ---- routed calls ----------------------------------------------------------------------------

def _hanging_relay(hang: str, answers: dict, seen: list):
    """`hang`'s request leaves (ON_SEND) and never answers; every other provider is scripted."""
    scripted = _relay_by_provider(answers, seen)

    async def _relay(request, upstream_url, *args, **kwargs):
        if hang in upstream_url:
            seen.append((hang,))
            if (on_send := relay_module.ON_SEND.get()) is not None:
                on_send()
            await asyncio.sleep(3600)
        return await scripted(request, upstream_url, *args, **kwargs)
    return _relay


async def _no_holds():
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


@pytest.fixture
def child_bound(monkeypatch):
    monkeypatch.setenv("TREG_ROUTE_CHILD_TIMEOUT_S", "0.3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_hanging_child_costs_its_bound_not_the_relay_timeout(clients, enrichment_on, child_bound,
                                                                      monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _hanging_relay("leadmagic", {"tomba": [TOMBA_MISS]}, seen))
    before = await _balance(clients)
    started = time.monotonic()
    r = await clients.post(f"/call/{ROUTED}", json={"email": "a@example.com"}, headers=HEADERS)
    assert time.monotonic() - started < 10
    assert r.status_code == 200, r.text
    doc = r.json()["_treg"]
    hung = doc["tried"][0]
    assert (hung["provider"], hung["outcome"], hung["status"], hung["charged_micro"]) == ("leadmagic", "error", 504, 0)
    assert "no answer within 0.3 s" in hung["detail"]
    assert doc["outcome"] == "miss" and doc["incomplete"] is True
    assert before - await _balance(clients) == doc["charged_micro"] == doc["tried"][1]["charged_micro"]
    await _no_holds()
    assert (await route_health.read([LEADMAGIC]))[LEADMAGIC].failed == 1


@pytest.fixture
def quick_trip(monkeypatch):
    monkeypatch.setenv("TREG_ROUTE_HEALTH_MIN_CALLS", "2")
    monkeypatch.setenv("TREG_ROUTE_HEALTH_PROBE_S", "3600")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fail(endpoint_id: str, n: int = 2) -> None:
    for _ in range(n):
        route_health.note(endpoint_id, failed=True, slow=False)


async def test_a_failing_child_is_skipped_once_its_probe_turn_is_taken(clients, enrichment_on, quick_trip,
                                                                       monkeypatch):
    _fail(LEADMAGIC)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"leadmagic": [(503, {"message": "down"})], "tomba": [TOMBA_MISS, TOMBA_MISS]}, seen))
    # the first call after the trip is the probe: asked where it ranks
    r = await clients.post(f"/call/{ROUTED}", json={"email": "a@example.com"}, headers=HEADERS)
    assert [s[0] for s in seen] == ["leadmagic", "tomba"]
    # the next one skips it, and says why
    seen.clear()
    r = await clients.post(f"/call/{ROUTED}", json={"email": "a@example.com"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert [s[0] for s in seen] == ["tomba"]
    doc = r.json()["_treg"]
    assert doc["outcome"] == "miss" and "incomplete" not in doc
    skipped = [d for d in doc["dropped"] if d["endpoint_id"] == LEADMAGIC]
    assert skipped and skipped[0]["health"] == "failing" and "failing right now" in skipped[0]["why"]


async def test_a_failing_child_is_still_asked_when_nothing_else_is_left(clients, enrichment_on, quick_trip,
                                                                        monkeypatch):
    for endpoint_id in (LEADMAGIC, TOMBA):
        _fail(endpoint_id)
        assert await route_health.probe(endpoint_id)   # spend the probe turn
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"leadmagic": [TOMBA_MISS], "tomba": [TOMBA_MISS]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"email": "a@example.com"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert {s[0] for s in seen} == {"leadmagic", "tomba"}


async def test_observe_mode_reports_but_never_skips(clients, enrichment_on, quick_trip, monkeypatch):
    monkeypatch.setenv("TREG_ROUTE_HEALTH", "observe")
    get_settings.cache_clear()
    _fail(LEADMAGIC)
    assert await route_health.probe(LEADMAGIC)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"leadmagic": [(503, {"message": "down"})], "tomba": [TOMBA_MISS]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"email": "a@example.com"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert [s[0] for s in seen] == ["leadmagic", "tomba"]


# ---- review fixes ----------------------------------------------------------------------------

def test_an_async_child_has_no_time_bound_once_it_may_hold_an_accepted_task():
    """A submission the provider accepted keeps its hold and answers 202 pending; cancelling the
    child after acceptance would release a hold a live task still owns. The bridge bounds the wait."""
    from treg.application.call import route as call_route
    sync, task = _cand("a"), _cand("b")
    task.endpoint["async"] = {"poll": {"endpoint": "b.poll"}}
    assert call_route._child_timeout_s(sync) is not None
    assert call_route._child_timeout_s(task) is None


def test_only_the_callers_own_prefer_outranks_health():
    """A contract default or a published rule names an order; a child judged slow or failing right
    now is still asked after the healthy ones. Only an explicit caller prefer keeps it first."""
    cands = [_cand("slow", health="slow", price=1), _cand("fine", price=2)]
    assert [c.endpoint["provider"] for c in rank(cands, prefer=["slow"])] == ["fine", "slow"]
    assert [c.endpoint["provider"] for c in rank(cands, prefer=["slow"], caller_prefer=True)] == ["slow", "fine"]


async def test_a_failing_child_is_kept_when_it_is_the_only_one_within_the_ceiling(quick_trip):
    from treg.application.call import route as call_route
    failing, dear = _cand("leadmagic", price=6_250), _cand("tomba", price=8_900)
    _fail(failing.endpoint["id"])
    assert await route_health.probe(failing.endpoint["id"])   # the probe turn is taken
    cands, dropped = [failing, dear], []
    await call_route._apply_health(cands, dropped, probe=True, max_cost_micro=7_500)
    assert failing in cands and failing.health == "failing" and dropped == []
    # with room for the other, it is dropped as before
    cands, dropped = [failing, dear], []
    failing.health = "ok"
    await call_route._apply_health(cands, dropped, probe=True, max_cost_micro=10_000)
    assert cands == [dear] and dropped[0]["health"] == "failing"
