"""`X-Treg-Route-Live` on a phone find: a number counts only once HLR's live lookup says LIVE.

Each number is looked up once, as a direct linked child `:l{n}` whose hold closes with the find's.
A number that is not live sends the waterfall on; when none is, the first hit comes back marked not
live. A candidate starts only while its price plus one lookup fits under the route cost stop."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_route_verify_phone import FIND, HLR, _hlr, _quickenrich, _relay, _tomba

ASK = {"linkedin_url": "https://www.linkedin.com/in/example"}
ON = {"X-Treg-Route-Live": "true"}
QE = "quickenrich.people.phone.find"   # the cheaper of the two, asked first
TOMBA = "tomba.people.phone.find"


@pytest.fixture
def phones_on(monkeypatch, platform_on):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA", "PLATFORM-TOMBA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_KEY_QUICKENRICH", "PLATFORM-QUICKENRICH-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP", "PLATFORM-HLR-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP_SECRET", "PLATFORM-HLR-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tomba,quickenrich,hlrlookup")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _settled() -> set[str]:
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == [], "every hold is closed"
        return {e.call_id for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"}


def _paid(d: dict) -> int:
    return sum(t["charged_micro"] for t in d["_treg"]["tried"]) + sum(c["cost_micro"] for c in d["_treg"]["live"]["checks"])


async def test_a_live_first_hit_ends_the_call(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"quickenrich": [_quickenrich("4155550142", "US")], "hlrlookup": [_hlr("LIVE", 2)]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p for p, _ in seen] == ["quickenrich", "hlrlookup"], "no other provider is asked"
    assert d["_treg"]["served_by"] == QE and d["output"]["phone"] == "+14155550142"
    live = d["_treg"]["live"]
    assert live["live"] is True and live["verdict"] == "live" and r.headers["X-Treg-Route-Line"] == "live"
    call_id = r.headers["X-Treg-Call-Id"]
    assert live["checks"] == [{"for": QE, "verdict": "live", "checked": True, "served_by": HLR,
                               "cost_micro": live["checks"][0]["cost_micro"], "call_id": f"{call_id}:l0"}]
    assert live["checks"][0]["cost_micro"] > 0
    assert seen[1][1]["cache_days_global"] == 0 and seen[1][1]["cache_days_private"] == 0, "a live lookup, no cache"
    assert "verification" not in d["_treg"], "only X-Treg-Route-Verify asks for the check view"
    assert "advice" not in d["_treg"], "the line was looked up: the advice to look it up is gone"
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == _paid(d) == before - await _balance(clients)
    assert await _settled() == {f"{call_id}:r0", f"{call_id}:l0"}


async def test_a_dead_number_walks_on_to_a_live_one(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "quickenrich": [_quickenrich("4155550142", "US")], "tomba": [_tomba("+447540822872")],
        "hlrlookup": [_hlr("DEAD", 2), _hlr("LIVE", 1)]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p for p, _ in seen] == ["quickenrich", "hlrlookup", "tomba", "hlrlookup"]
    assert d["_treg"]["served_by"] == TOMBA and d["output"]["phone"] == "+447540822872"
    assert d["_treg"]["live"]["live"] is True
    assert [(c["for"], c["verdict"]) for c in d["_treg"]["live"]["checks"]] == [(QE, "dead"), (TOMBA, "live")]
    assert d["_treg"]["tried"][0]["detail"] == "line dead; walked on"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["hit", "hit"]
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == _paid(d) == before - await _balance(clients), "both finds and both lookups are billed"
    call_id = r.headers["X-Treg-Call-Id"]
    assert await _settled() == {f"{call_id}:r0", f"{call_id}:l0", f"{call_id}:r1", f"{call_id}:l1"}


async def test_no_live_number_returns_the_first_hit_marked_not_live(clients: AsyncClient, phones_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay({
        "quickenrich": [_quickenrich("4155550142", "US")], "tomba": [_tomba("+447540822872")],
        "hlrlookup": [_hlr("DEAD", 2), _hlr("ABSENT_SUBSCRIBER", 1)]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["outcome"] == "hit" and d["_treg"]["served_by"] == QE, "never fewer answers"
    assert d["output"]["phone"] == "+14155550142"
    assert d["_treg"]["live"]["live"] is False and d["_treg"]["live"]["verdict"] == "dead"
    assert r.headers["X-Treg-Route-Line"] == "dead"
    assert int(r.headers["X-Treg-Cost-Micro"]) == _paid(d)


async def test_one_number_is_looked_up_once(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "quickenrich": [_quickenrich("4155550142", "US")], "tomba": [_tomba("+1 415 555 0142")],
        "hlrlookup": [_hlr("DEAD", 2)]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    assert [p for p, _ in seen] == ["quickenrich", "hlrlookup", "tomba"], "the same line is not paid for twice"
    assert len(r.json()["_treg"]["live"]["checks"]) == 1 and r.json()["_treg"]["served_by"] == QE


async def test_a_number_without_a_country_code_is_not_looked_up_and_walks_on(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "quickenrich": [_quickenrich("4155550142", "GB")], "tomba": [_tomba("+447540822872")],
        "hlrlookup": [_hlr("LIVE", 1)]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p for p, _ in seen] == ["quickenrich", "tomba", "hlrlookup"]
    first = d["_treg"]["live"]["checks"][0]
    assert first["checked"] is False and first["reason"] == "not_international" and "call_id" not in first
    assert d["_treg"]["tried"][0]["detail"] == "line not checked: not_international; walked on"
    assert d["_treg"]["served_by"] == TOMBA and d["_treg"]["live"]["live"] is True


async def test_with_verify_the_winners_lookup_is_the_check(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "quickenrich": [_quickenrich("4155550142", "US")], "hlrlookup": [_hlr("LIVE", 2)]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Verify": "true"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p for p, _ in seen] == ["quickenrich", "hlrlookup"], "the line is looked up once, not twice"
    v = d["_treg"]["verification"]
    assert v == {k: x for k, x in d["_treg"]["live"]["checks"][0].items() if k not in ("for", "call_id")}
    assert v["verdict"] == "live"
    assert int(r.headers["X-Treg-Cost-Micro"]) == _paid(d)


async def test_the_lookup_counts_against_the_cost_stop(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "quickenrich": [_quickenrich("4155550142", "US")], "hlrlookup": [_hlr("DEAD", 2)]}, seen))
    # QuickEnrich ($0.0048) plus one lookup fits $0.05; Tomba ($0.0445) plus one more does not.
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Max-Cost": "0.05"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p for p, _ in seen] == ["quickenrich", "hlrlookup"]
    assert d["_treg"]["tried"][1] == {"endpoint_id": TOMBA, "provider": "tomba", "outcome": "skipped",
                                      "status": None, "charged_micro": 0, "detail": "would exceed max cost"}
    assert d["_treg"]["capped"] is True and d["_treg"]["live"]["live"] is False
    assert d["_treg"]["charged_micro"] <= 50_000


async def test_the_header_needs_the_waterfall(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_live_unsupported"
    assert seen == [] and await _balance(clients) == before, "refused before anything is held"


async def test_a_contract_without_a_live_check_refuses_the_header(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    r = await clients.post("/call/treg.people.email.find", json={"email": "p@stripe.com"}, headers=ON)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_live_unsupported"
    assert "treg.people.phone.find" in r.json()["detail"]["message"] and seen == []


async def test_without_the_header_nothing_is_looked_up(clients: AsyncClient, phones_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"quickenrich": [_quickenrich("4155550142", "US")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    assert [p for p, _ in seen] == ["quickenrich"] and "live" not in r.json()["_treg"]
    assert "X-Treg-Route-Line" not in r.headers
