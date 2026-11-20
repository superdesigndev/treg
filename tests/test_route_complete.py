"""`X-Treg-Route-Complete` on `treg.linkedin.user.profile`.

Opt-in. An adapter that cannot map every field the contract lists as `complete` is asked last; an
answer missing one is weak (keep going, the fullest wins if nothing better turns up); and an
answer whose profile identifier differs from the one asked counts only once another provider
reports the same identifier (a renamed profile), never on its own (a match on someone else).
Measured 2026-10-29 on 278 live LinkedIn URLs: net correct fields 79.7 today, 95.6 with the header."""

from __future__ import annotations

import copy

import pytest

from toon_format import decode

from treg import cli
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store

from test_cli import _CatalogResp, _stub_catalog_client
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


@pytest.fixture
def profile_with_scrape(monkeypatch, platform_on):
    for p in ("ANYAPI", "FETCHINIO", "HARVESTAPI", "SCRAPECREATORS"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "anyapi,fetchinio,harvestapi,scrapecreators")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fetchin(url: str = ASKED) -> dict:
    doc = copy.deepcopy(_example("fetchinio.linkedin.user.profile"))
    doc["url"] = url
    return doc


def _anyapi_full(url: str) -> dict:
    doc = copy.deepcopy(_example("anyapi.linkedin.user.profile.full"))
    doc["output"]["data"]["url"] = url
    return doc


def _anyapi_thin(*, headline: str | None = None, public_id: str | None = None) -> dict:
    """Thin anyapi body; headline/publicIdentifier omitted from the captured example but returned live."""
    doc = copy.deepcopy(_example("anyapi.linkedin.user.profile"))
    data = doc["output"]["data"]
    data["location"] = "Seattle, Washington, United States"
    if headline is not None:
        data["headline"] = headline
    if public_id is not None:
        data["publicIdentifier"] = public_id
    return doc


THIN = _anyapi_thin()
THIN_FULL = _anyapi_thin(headline="Chair, Gates Foundation", public_id="williamhgates")
NOT_FOUND = (404, {"error": "LinkedIn profile not found", "code": "PROFILE_NOT_FOUND"})


async def test_without_the_header_the_cheapest_thin_answer_ends_the_route(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"anyapi": [(200, THIN)]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "anyapi.linkedin.user.profile" and len(seen) == 1


async def test_without_the_header_a_mapped_headline_still_ends_the_route(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"anyapi": [(200, THIN_FULL)]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "anyapi.linkedin.user.profile" and len(seen) == 1
    assert d["output"]["headline"] == "Chair, Gates Foundation"
    assert d["output"]["linkedin_url"] == "https://www.linkedin.com/in/williamhgates"


async def test_with_the_header_a_complete_thin_answer_ends_the_route(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"anyapi": [(200, THIN_FULL)]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "anyapi.linkedin.user.profile" and len(seen) == 1
    assert d["_treg"]["tried"][0]["outcome"] == "hit"


async def test_an_adapter_that_cannot_answer_fully_is_asked_last(clients, profile_with_scrape, monkeypatch):
    ad = store.load().adapters
    assert "headline" not in ad["scrapecreators.linkedin.user.profile"].out_map
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"anyapi": [(200, THIN_FULL)], "scrape": [(200, _example("scrapecreators.linkedin.user.profile"))],
         "fetchin": [NOT_FOUND], "harvest": [NOT_FOUND] * 2}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "anyapi.linkedin.user.profile"
    assert "scrapecreators" not in {p for p, *_ in seen}, "incapable adapters stay behind a complete hit"


async def test_a_thin_answer_is_weak_and_still_returned_when_nothing_fuller_turns_up(clients, profile_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"anyapi": [(200, THIN), (200, {"output": {"found": False}})],
         "fetchin": [NOT_FOUND], "harvest": [NOT_FOUND] * 2}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "anyapi.linkedin.user.profile", "the fullest weak answer beats nothing"
    assert d["_treg"]["tried"][0]["outcome"] == "weak" and "headline" in d["_treg"]["tried"][0]["detail"]


async def test_another_identifier_needs_a_second_provider(clients, profile_on, monkeypatch):
    renamed = "https://www.linkedin.com/in/bill-gates-renamed"
    thin_renamed = _anyapi_thin(headline="Chair", public_id="bill-gates-renamed")
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"anyapi": [(200, thin_renamed)], "fetchin": [(200, _fetchin(renamed))]}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "anyapi.linkedin.user.profile", "confirmed: the earlier, cheaper answer"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["weak", "weak"] and len(seen) == 2


async def test_another_identifier_nobody_confirms_is_not_returned(clients, profile_on, monkeypatch):
    seen = []
    someone = _anyapi_full("https://www.linkedin.com/in/someone-else")
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"anyapi": [(200, {"output": {"found": False}}), (200, someone)],
         "fetchin": [NOT_FOUND], "harvest": [NOT_FOUND] * 2}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    d = r.json()
    assert d.get("_treg", {}).get("served_by") != "anyapi.linkedin.user.profile.full", r.text


async def test_the_contract_names_what_a_complete_answer_carries(clients, profile_on, monkeypatch):
    """Most fields but no headline (a scraper that reads only the top card): still weak, because the
    contract's `complete` list names the headline. Measured 2026-10-29: such answers on 20 of 278
    people cost 3 points of net correct fields."""
    assert store.load().contracts["linkedin.user.profile"].complete == ("full_name", "headline", "location")
    headless = _fetchin()
    headless["title"] = None
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"anyapi": [(200, THIN), (200, _anyapi_full(ASKED))], "fetchin": [(200, headless)],
         "harvest": [NOT_FOUND] * 2}, seen))
    r = await clients.post(PROFILE, json={"linkedin_url": ASKED}, headers=COMPLETE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "anyapi.linkedin.user.profile.full"
    assert d["_treg"]["tried"][0]["outcome"] == "weak" and "headline" in d["_treg"]["tried"][0]["detail"]


async def test_catalog_get_shows_both_answer_bars_over_the_api_and_the_cli(clients, profile_on, monkeypatch, capsys):
    r = await clients.get("/catalog/endpoints/treg.linkedin.user.profile")
    assert r.status_code == 200, r.text
    routing = r.json()["routing"]
    assert "routing.contract.complete" in routing["headers"]["X-Treg-Route-Complete"]
    assert routing["contract"]["complete"] == ["full_name", "headline", "location"]

    _stub_catalog_client(monkeypatch, {"/catalog/endpoints/treg.linkedin.user.profile": _CatalogResp(r.json())})
    cli.cmd_catalog(cli.build_parser().parse_args(["catalog", "get", "treg.linkedin.user.profile"]),
                    {"base_url": "http://x"})
    out = decode(capsys.readouterr().out)
    assert out["stops_at"] == "the first answer unless full_name == null"
    assert out["complete"].startswith("full_name, headline, location (X-Treg-Route-Complete: 1")
