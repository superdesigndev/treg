"""`X-Treg-Route-Complete` on `treg.linkedin.user.profile`.

Opt-in. An adapter that maps under half the contract's output fields is asked last; an answer that
carries under half of them is weak (keep going, the fullest wins if nothing better turns up); and an
answer whose profile identifier differs from the one asked counts only once another provider
reports the same identifier (a renamed profile), never on its own (a match on someone else).
Measured 2026-10-29 on 278 live LinkedIn URLs: net correct fields 79.7 today, 95.6 with the header."""

from __future__ import annotations

import copy

import pytest

from treg.application.call import service as call_service
from treg.config import get_settings

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _example, _relay_by_provider

PROFILE = "/call/treg.linkedin.user.profile"
ASKED = "https://www.linkedin.com/in/williamhgates"
COMPLETE = {"X-Treg-Route-Complete": "1"}


@pytest.fixture
def profile_on(monkeypatch, platform_on):
    for p in ("ANYAPI", "FETCHINIO", "HARVESTAPI"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "anyapi,fetchinio,harvestapi")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fetchin(url: str = ASKED) -> dict:
    doc = copy.deepcopy(_example("fetchinio.linkedin.user.profile"))
    doc["url"] = url
    return doc


def _harvest(url: str) -> dict:
    doc = copy.deepcopy(_example("harvestapi.linkedin.user.profile.main"))
    doc["element"]["linkedinUrl"] = url
    return doc


def _anyapi_full(url: str) -> dict:
    doc = copy.deepcopy(_example("anyapi.linkedin.user.profile.full"))
    doc["output"]["data"]["url"] = url
    return doc


THIN = _example("anyapi.linkedin.user.profile")      # name, location, followers, about: 4 of 9 fields
NOT_FOUND = (404, {"error": "LinkedIn profile not found", "code": "PROFILE_NOT_FOUND"})


async def test_without_the_header_the_cheapest_thin_answer_ends_the_route(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"anyapi": [(200, THIN)]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "anyapi.linkedin.user.profile" and len(seen) == 1


async def test_an_adapter_that_cannot_answer_fully_is_asked_last(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"fetchin": [(200, _fetchin())]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "fetchinio.linkedin.user.profile"
    plan = [t["endpoint_id"] for t in r.json()["_treg"]["tried"]]
    assert plan == ["fetchinio.linkedin.user.profile"]


async def test_a_thin_answer_is_weak_and_still_returned_when_nothing_fuller_turns_up(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"fetchin": [NOT_FOUND], "harvest": [NOT_FOUND] * 2, "anyapi": [(200, {"output": {"found": False}}), (200, THIN)]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "anyapi.linkedin.user.profile", "the fullest weak answer beats nothing"
    assert [t["outcome"] for t in d["_treg"]["tried"]][-1] == "weak"


async def test_another_identifier_needs_a_second_provider(clients, profile_on, monkeypatch):
    renamed = "https://www.linkedin.com/in/bill-gates-renamed"
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"fetchin": [(200, _fetchin(renamed))], "harvest": [(200, _harvest(renamed))]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "fetchinio.linkedin.user.profile", "confirmed: the earlier, cheaper answer"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["weak", "weak"] and len(seen) == 2


async def test_another_identifier_nobody_confirms_is_not_returned(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"fetchin": [NOT_FOUND], "anyapi": [(200, {"output": {"found": False}}), (200, _anyapi_full(
            "https://www.linkedin.com/in/someone-else"))], "harvest": [NOT_FOUND] * 2}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    d = r.json()
    assert d.get("_treg", {}).get("served_by") != "anyapi.linkedin.user.profile.full", r.text


async def test_the_contract_names_what_a_complete_answer_carries(clients, profile_on, monkeypatch):
    """Most fields but no headline (a scraper that reads only the top card): still weak, because the
    contract's `complete` list names the headline. Measured 2026-10-29: such answers on 20 of 278
    people cost 3 points of net correct fields."""
    from treg.domain.catalog import store
    assert store.load().contracts["linkedin.user.profile"].complete == ("full_name", "headline", "location")
    headless = _fetchin()
    headless["title"] = None
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"fetchin": [(200, headless)], "harvest": [(200, _harvest(ASKED))]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "harvestapi.linkedin.user.profile.main"
    assert d["_treg"]["tried"][0]["outcome"] == "weak" and "headline" in d["_treg"]["tried"][0]["detail"]
