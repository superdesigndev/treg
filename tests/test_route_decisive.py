"""`X-Treg-Route-Decisive`: an email check whose answer counts only when it decides (valid / invalid).

Any other verdict word, or a failed attempt, sends the call on - at most two more providers, each
billed at its own price - and the providers left are asked in order of measured verdict quality
where it is known. When none decides, the first answer comes back, with `_treg.decision` saying so."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient

from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.domain.catalog.routing import quality
from treg.infra.route_quality import RouteQualityCache

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_route_verify import _bb, _relay, verifiers_on  # noqa: F401

VERIFY = "treg.people.email.verify"
ADDRESS = {"email": "patrick@stripe.com"}
ON = {"X-Treg-Route-Decisive": "true"}


def _word(status: str | None) -> tuple[int, dict]:
    """Tomba and Hunter verify answers carrying the provider's own word."""
    return 200, {"data": {"email": {"status": status, "score": 50}, "status": status, "score": 50}}


def _quality(rows: dict[str, tuple[float, float]], slice_: str = "all") -> quality.Snapshot:
    return {slice_: {e: quality.VerdictQuality(d, a, 1000) for e, (d, a) in rows.items()}}


class _Reader:
    def __init__(self, snap):
        self.snap = snap

    async def get(self, capability):
        return self.snap


async def test_without_the_header_an_undecided_word_ends_the_call(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"bounceban": [_bb("unknown")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS)
    assert r.status_code == 200, r.text
    assert seen == ["bounceban"] and "decision" not in r.json()["_treg"]
    assert "X-Treg-Route-Decided" not in r.headers


async def test_a_deciding_first_answer_ends_the_call(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"bounceban": [_bb("undeliverable")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
    assert r.status_code == 200, r.text
    assert seen == ["bounceban"]
    assert r.json()["_treg"]["decision"] == {"decided": True, "decided_by": "bounceban", "ordered_by": "price"}
    assert r.headers["X-Treg-Route-Decided"] == "true"


async def test_an_undecided_word_goes_on_and_the_next_decision_is_the_answer(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"bounceban": [_bb("unknown")], "tomba-verify": [_word("valid")], "hunter-verify": [_word("valid")]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen[0] == "bounceban" and len(seen) == 2
    assert d["_treg"]["provider"] == seen[1].removesuffix("-verify") and d["output"]["valid"] is True
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["weak", "hit"]
    assert d["_treg"]["decision"]["decided"] is True
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == sum(t["charged_micro"] for t in d["_treg"]["tried"])
    assert before - await _balance(clients) == total, "both checks are billed"


async def test_measured_quality_orders_the_providers_left(clients: AsyncClient, verifiers_on, monkeypatch):
    for best, other in (("hunter", "tomba"), ("tomba", "hunter")):
        seen = []
        monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(_quality(
            {f"{best}.people.email.verify": (0.95, 0.99), f"{other}.people.email.verify": (0.6, 0.9)})))
        monkeypatch.setattr(call_service, "relay", _relay(
            {"bounceban": [_bb("unknown")], f"{best}-verify": [_word("invalid")], f"{other}-verify": [_word("valid")]}, seen))
        r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
        assert r.status_code == 200, r.text
        assert seen == ["bounceban", f"{best}-verify"]
        assert r.json()["_treg"]["decision"] == {"decided": True, "decided_by": best, "ordered_by": "quality"}


async def test_a_catch_all_word_orders_by_the_catch_all_slice(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    snap = {**_quality({"tomba.people.email.verify": (0.9, 0.99), "hunter.people.email.verify": (0.5, 0.9)}),
            **_quality({"hunter.people.email.verify": (0.9, 0.99), "tomba.people.email.verify": (0.1, 0.9)}, "catch_all")}
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(snap))
    monkeypatch.setattr(call_service, "relay", _relay(
        {"bounceban": [_bb("risky", is_accept_all=True)], "hunter-verify": [_word("valid")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
    assert r.status_code == 200, r.text
    assert seen == ["bounceban", "hunter-verify"], "the catch-all slice put hunter first"


async def test_nobody_deciding_returns_the_first_answer_undecided(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"bounceban": [_bb("unknown")], "tomba-verify": [_word("accept_all")], "hunter-verify": [_word("webmail")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(seen) == 3 and d["_treg"]["provider"] == "bounceban", "the first, already-paid answer"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["weak", "weak", "weak"]
    assert d["_treg"]["decision"]["decided"] is False and r.headers["X-Treg-Route-Decided"] == "false"


async def test_a_callers_prefer_order_is_kept(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(_quality(
        {"tomba.people.email.verify": (0.99, 0.99), "bounceban.people.email.verify": (0.5, 0.9)})))
    monkeypatch.setattr(call_service, "relay", _relay(
        {"hunter-verify": [_word("webmail")], "bounceban": [_bb("deliverable")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS,
                           headers={**ON, "X-Treg-Route-Prefer": "hunter,bounceban"})
    assert r.status_code == 200, r.text
    assert seen == ["hunter-verify", "bounceban"]


async def test_the_header_needs_the_waterfall_and_deciding_words(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers={**ON, "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_decisive_unsupported"
    r = await clients.post("/call/treg.people.email.find", json={"full_name": "Patrick Collison", "domain": "stripe.com"},
                           headers=ON)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_decisive_unsupported"
    assert VERIFY in r.json()["detail"]["message"]
    assert seen == [] and await _balance(clients) == before, "refused before anything is held"


def test_quality_parse_and_order():
    snap = quality.parse({"slices": {"all": {"a": {"decisive_rate": 0.9, "agreement": 0.99, "comparable": 500},
                                             "b": {"decisive_rate": 1.0, "agreement": 0.9, "comparable": 500},
                                             "thin": {"decisive_rate": 1.0, "agreement": 1.0, "comparable": 10},
                                             "broken": {"decisive_rate": None}},
                                     "nonsense": {"x": {}}}})
    assert set(snap) == {"all"} and set(snap["all"]) == {"a", "b", "thin"}
    assert round(snap["all"]["a"].expected_net, 3) == 0.882 and round(snap["all"]["b"].expected_net, 3) == 0.8
    assert quality.by_quality(["thin", "x", "b", "a"], lambda e: e, snap, "all") == ["a", "b", "thin", "x"]
    snap["catch_all"] = {"b": quality.VerdictQuality(0.5, 0.9, 300)}
    assert quality.by_quality(["thin", "a", "b"], lambda e: e, snap, "catch_all") == ["b", "a", "thin"], \
        "rated on the slice beats a better number on all calls"
    assert quality.by_quality(["b", "a"], lambda e: e, None, "all") == ["b", "a"], "no data keeps the given order"


async def test_quality_cache_serves_stale_and_survives_failures():
    calls, now = [], [0.0]

    async def fetch(cap):
        calls.append(cap)
        if len(calls) == 2:
            raise RuntimeError("store down")
        return b'{"slices": {"all": {"a": {"decisive_rate": 1, "agreement": 1, "comparable": 300}}}}'

    cache = RouteQualityCache(fetch, ttl_s=10, clock=lambda: now[0])
    first = await cache.get("c")
    assert first["all"]["a"].comparable == 300 and await cache.get("c") is first and calls == ["c"]
    now[0] = 11
    assert await cache.get("c") is first, "stale is served while the refresh runs"
    await asyncio.sleep(0)
    assert calls == ["c", "c"] and await cache.get("c") is first, "a failed refresh keeps the last value"


def test_a_file_may_lower_the_bar_but_not_below_the_floor():
    row = {"decisive_rate": 0.5, "agreement": 0.9, "comparable": 40}
    assert quality.rated(quality.parse({"slices": {"all": {"a": row}}}), "a", "all") is None, "default bar is 200"
    assert quality.rated(quality.parse({"min_comparable": 30, "slices": {"all": {"a": row}}}), "a", "all").comparable == 40
    low = quality.parse({"min_comparable": 5, "slices": {"all": {"a": {**row, "comparable": 20}}}})
    assert quality.rated(low, "a", "all") is None, "never below the floor"
    high = quality.parse({"min_comparable": 10_000, "slices": {"all": {"a": {**row, "comparable": 250}}}})
    assert quality.rated(high, "a", "all") is not None, "a file cannot raise the bar above the default"


async def test_a_warmed_cache_answers_the_first_call_without_waiting(monkeypatch):
    from treg.infra import route_quality as infra_quality
    calls = []

    async def fetch(cap):
        calls.append(cap)
        return b'{"slices": {"all": {"a": {"decisive_rate": 1, "agreement": 1, "comparable": 300}}}}'

    cache = RouteQualityCache(fetch, ttl_s=10)
    cache.warm(["c", "d"])
    await asyncio.sleep(0)
    monkeypatch.setattr(infra_quality, "FIRST_READ_WAIT_S", 0.0)
    assert (await cache.get("c"))["all"]["a"].comparable == 300
    assert sorted(calls) == ["c", "d"], "each file is read once, at warm-up"


class _Broken:
    def __init__(self):
        self.calls = 0

    async def get(self, capability):
        self.calls += 1
        raise RuntimeError("store down")


async def test_without_the_header_the_quality_reader_is_never_asked(clients: AsyncClient, verifiers_on, monkeypatch):
    seen, reader = [], _Broken()
    monkeypatch.setattr(call_route, "_route_quality_reader", reader)
    monkeypatch.setattr(call_service, "relay", _relay({"bounceban": [_bb("unknown")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS)
    assert r.status_code == 200 and seen == ["bounceban"] and reader.calls == 0


async def test_an_unreadable_quality_store_keeps_price_order(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Broken())
    monkeypatch.setattr(call_service, "relay", _relay(
        {"bounceban": [_bb("unknown")], "tomba-verify": [_word("valid")], "hunter-verify": [_word("valid")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
    assert r.status_code == 200, r.text
    assert len(seen) == 2 and r.json()["_treg"]["decision"] == {
        "decided": True, "decided_by": seen[1].removesuffix("-verify"), "ordered_by": "price"}


async def test_a_failing_reorder_keeps_the_order_it_had(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(_quality({"tomba.people.email.verify": (0.9, 0.99)})))

    def boom(*args, **kwargs):
        raise ValueError("bad number")
    monkeypatch.setattr(quality, "by_quality", boom)
    monkeypatch.setattr(call_service, "relay", _relay(
        {"bounceban": [_bb("unknown")], "tomba-verify": [_word("valid")], "hunter-verify": [_word("valid")]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json=ADDRESS, headers=ON)
    assert r.status_code == 200, r.text
    assert len(seen) == 2 and r.json()["_treg"]["decision"]["ordered_by"] == "price"


def test_parse_never_raises_on_odd_files():
    for doc in (None, [], "x", {"slices": []}, {"slices": {"all": []}}, {"slices": {"all": {"a": []}}},
                {"slices": {"all": {"a": {"decisive_rate": 2, "agreement": 0.9, "comparable": 500}}}},
                {"slices": {"all": {"a": {"decisive_rate": "x", "agreement": 0.9, "comparable": 500}}}}):
        assert quality.parse(doc) == {}
