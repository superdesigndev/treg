"""A routed people search neither re-bills page one for a page it cannot send, nor lets Exa's
per-result rider pass X-Treg-Route-Max-Cost on a base-price hold."""

import json

import pytest

from treg import call_events as sink
from treg.application.call import service
from treg.config import get_settings

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_call_events import SECRET
from test_routing import _relay_by_provider


@pytest.fixture
def exa_on(monkeypatch, platform_on):
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "K-exa")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "exa")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_paging_key_is_refused_before_any_call(clients, exa_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"*": [(200, {})]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json={"title": "VP Sales", "page": 2})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "route_paging_unsupported"
    assert seen == [] and await _balance(clients) == before


def _exa(n):
    return {"results": [{"url": f"https://linkedin.com/in/p{i}", "title": f"P {i}"} for i in range(n)],
            "costDollars": {"total": round(0.007 + 0.001 * max(0, n - 10), 6)}}


@pytest.mark.parametrize("limit,cap,status,charge", [(100, "0.05", 402, 0), (10, "0.05", 200, 7_000), (100, "1", 200, 97_000)])
async def test_exa_rider_is_held_against_the_ceiling(clients, exa_on, monkeypatch, limit, cap, status, charge):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"exa": [(200, _exa(limit))]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json={"q": "VP Sales at fintechs", "limit": limit},
                           headers={"X-Treg-Route-Max-Cost": cap, "X-Treg-Route-Exclude": "parallel,crawl4ai"})
    assert r.status_code == status, r.text
    charged = before - await _balance(clients)
    assert charged == charge <= float(cap) * 1_000_000
    assert (seen == []) == (status == 402)


@pytest.fixture
def lists_on(monkeypatch, platform_on):
    for p in ("QUICKENRICH", "DROPLEADS"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"K-{p}")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "quickenrich,dropleads")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _qe(n, cursor):
    return {"data": [{"first_name": "P", "last_name": str(i), "employee_linkedin": f"https://linkedin.com/in/q{i}"}
                     for i in range(n)], "meta": {"next_cursor": cursor}}


def _dl(n, total):
    return {"data": {"leads": [{"fullName": f"D {i}", "linkedinUrl": f"https://linkedin.com/in/d{i}"}
                               for i in range(n)], "total": total}}


async def _page(clients, body, prefer):
    r = await clients.post("/call/treg.people.search", json=body, headers={"X-Treg-Route-Prefer": prefer})
    assert r.status_code == 200, r.text
    return r.json()


async def test_cursor_pages_through_the_provider_that_served_page_one(clients, lists_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"quickenrich": [(200, _qe(3, "QE-2")), (200, _qe(3, None))], "*": []}, seen))
    body = {"title": "VP Sales", "limit": 3}
    first = await _page(clients, body, "quickenrich")
    cursor = first["output"]["next_cursor"]
    assert cursor.startswith("trg1.") and first["raw"]["meta"]["next_cursor"] == "QE-2"
    # A later answer from page one's provider only, even with another preferred.
    second = await _page(clients, {**body, "cursor": cursor}, "dropleads")
    assert [s[0] for s in seen] == ["quickenrich", "quickenrich"]
    assert seen[1][3]["cursor"] == "QE-2" and "cursor" not in seen[0][3]
    assert second["output"]["next_cursor"] is None and len(second["output"]["people"]) == 3


async def test_a_long_cursor_from_a_paging_provider_is_named_on_treg(clients, lists_on, monkeypatch):
    """A paging child's over-long token still uses the long-cursor fallback: output.next_cursor
    is null, raw keeps the token, and _treg.next_cursor names the path. Wrapping first and
    dropping the token would make that null look like exhaustion."""
    from treg.application.call import route as call_route
    token = "Q" * (call_route.CURSOR_MAX + 1)
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"quickenrich": [(200, _qe(3, token))], "*": []}, []))
    answer = await _page(clients, {"title": "VP Sales", "limit": 3}, "quickenrich")
    assert answer["output"]["next_cursor"] is None
    assert answer["raw"]["meta"]["next_cursor"] == token
    assert answer["_treg"]["next_cursor"] == {"chars": len(token), "in_raw": "meta.next_cursor"}


async def test_a_provider_that_cannot_page_names_the_ones_that_can(clients, lists_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"dropleads": [(200, _dl(2, 4))], "*": []}, seen))
    answer = await _page(clients, {"title": "VP Sales", "limit": 2}, "dropleads")
    assert answer["output"].get("next_cursor") is None
    assert "X-Treg-Route-Prefer: quickenrich" in answer["_treg"]["paging"]


@pytest.mark.parametrize("change", [{"title": "CFO"}, "tamper"])
async def test_cursor_for_another_question_is_refused(clients, lists_on, monkeypatch, change):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"quickenrich": [(200, _qe(3, "QE-2"))], "*": []}, seen))
    body = {"title": "VP Sales", "limit": 3}
    cursor = (await _page(clients, body, "quickenrich"))["output"]["next_cursor"]
    nxt = {**body, "cursor": cursor + "x"} if change == "tamper" else {**body, **change, "cursor": cursor}
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json=nxt)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_cursor_invalid", r.text
    assert len(seen) == 1 and await _balance(clients) == before


async def test_a_routed_continuation_is_a_later_page_in_its_call_event(clients, lists_on, monkeypatch):
    monkeypatch.setenv("TREG_CALL_EVENTS_URL", "https://stream.example.invalid")
    monkeypatch.setenv("TREG_CALL_EVENTS_HMAC_SECRET", SECRET)
    get_settings.cache_clear()
    sink.reset()
    sent: list[dict] = []

    async def capture(batch, **_):
        sent.extend(json.loads(b"[" + b",".join(batch) + b"]"))

    monkeypatch.setattr(sink, "_post", capture)
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"quickenrich": [(200, _qe(3, "QE-2")), (200, _qe(3, None))], "*": []}, []))
    body = {"title": "VP Sales", "limit": 3}
    first = await _page(clients, body, "quickenrich")
    await _page(clients, {**body, "cursor": first["output"]["next_cursor"]}, "quickenrich")
    await sink.drain()
    sink.reset()
    parents = [e for e in sent if e["endpoint_id"] == "treg.people.search" and not e.get("parent_ref")]
    assert len(parents) == 2 and all(p.get("input_id") for p in parents)
    assert parents[0]["input_id"] != parents[1]["input_id"]
    assert parents[1]["input_meta"]["page_kind"] == "cursor" and "page_kind" not in parents[0]["input_meta"]
