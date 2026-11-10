"""`X-Treg-Route-Quality` and `X-Treg-Route-Upgrade` on `treg.people.enrich`.

Quality asks the plan in order of measured answer quality for the identity sent (the quality file's
slice for that variant), the unrated behind in price order. Upgrade re-asks the same routed
endpoint by the identity key the hit carries (a LinkedIn URL for an email), ordered by quality, and
keeps that answer only when it is a hit about the same person. Both are opt-in."""

from __future__ import annotations

import copy

import pytest

from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog.routing import quality

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _example, _relay_by_provider

ENRICH = "/call/treg.people.enrich"
LINKEDIN = {"linkedin_url": "https://www.linkedin.com/in/alex-example"}
EMAIL = {"email": "alex@example.com"}


@pytest.fixture
def enrich_on(monkeypatch, platform_on):
    for p in ("BLITZAPI", "HARVESTAPI"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "blitzapi,harvestapi")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Reader:
    def __init__(self, snap):
        self.snap = snap

    async def get(self, capability):
        return self.snap


def _rated(rows: dict[str, tuple[float, float]], slice_: str) -> quality.Snapshot:
    return {slice_: {e: quality.VerdictQuality(d, a, 1000) for e, (d, a) in rows.items()}}


HARVEST_FIRST = _rated({"harvestapi.linkedin.user.profile": (0.78, 0.99), "blitzapi.people.enrich": (0.8, 0.8)},
                       "linkedin_url")


def _named(eid: str, first: str, last: str) -> dict:
    doc = copy.deepcopy(_example(eid))
    person = doc.get("person") or doc.get("element")
    if "person" in doc:
        person.update(first_name=first, last_name=last, full_name=f"{first} {last}")
    else:
        person.update(firstName=first, lastName=last)
    return doc


async def test_without_the_header_the_price_order_stands(clients, enrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(HARVEST_FIRST))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"blitz": [(200, _example("blitzapi.people.enrich"))]}, seen))
    r = await clients.post(ENRICH, json=LINKEDIN)
    assert r.status_code == 200, r.text
    assert [s[0] for s in seen] == ["blitz"] and "ordered_by" not in r.json()["_treg"]


async def test_measured_quality_for_the_identity_orders_the_plan(clients, enrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(HARVEST_FIRST))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"harvest": [(200, _example("harvestapi.linkedin.user.profile"))]}, seen))
    r = await clients.post(ENRICH, json=LINKEDIN, headers={"X-Treg-Route-Quality": "1"})
    assert r.status_code == 200, r.text
    assert [s[0] for s in seen] == ["harvest"]
    assert r.json()["_treg"]["served_by"] == "harvestapi.linkedin.user.profile"
    assert r.json()["_treg"]["ordered_by"] == "quality"


async def test_another_identitys_slice_does_not_order_this_one(clients, enrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(
        _rated({"harvestapi.linkedin.user.profile": (0.78, 0.99)}, "email")))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"blitz": [(200, _example("blitzapi.people.enrich"))]}, seen))
    r = await clients.post(ENRICH, json=LINKEDIN, headers={"X-Treg-Route-Quality": "1"})
    assert r.status_code == 200, r.text
    assert [s[0] for s in seen] == ["blitz"], "unrated on linkedin_url: price order"


async def test_an_upgrade_by_linkedin_url_replaces_the_answer_about_the_same_person(clients, enrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(HARVEST_FIRST))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "blitz": [(200, _named("blitzapi.people.email.reverse", "Alex", "Example"))],
        "harvest": [(200, _named("harvestapi.linkedin.user.profile", "alex", "EXAMPLE"))]}, seen))
    before = await _balance(clients)
    r = await clients.post(ENRICH, json=EMAIL, headers={"X-Treg-Route-Upgrade": "linkedin_url"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [s[0] for s in seen] == ["blitz", "harvest"], "the upgrade is ordered by quality"
    assert d["_treg"]["served_by"] == "harvestapi.linkedin.user.profile"
    assert d["_treg"]["provider"] == "harvestapi" and "element" in d["raw"]
    up = d["_treg"]["upgrade"]
    assert up["upgraded"] is True and up["via"] == "linkedin_url" and up["cost_micro"] > 0
    assert d["output"]["company"], "fields the upgrade does not carry keep the first answer's"
    assert r.headers["X-Treg-Route-Upgraded"] == "true"
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] and before - await _balance(clients) == total, "both calls are billed"


async def test_an_upgrade_about_another_name_keeps_the_first_answer(clients, enrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(HARVEST_FIRST))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "blitz": [(200, _named("blitzapi.people.email.reverse", "Alex", "Example"))],
        "harvest": [(200, _named("harvestapi.linkedin.user.profile", "Sam", "Other"))]}, seen))
    r = await clients.post(ENRICH, json=EMAIL, headers={"X-Treg-Route-Upgrade": "linkedin_url"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "blitzapi.people.email.reverse" and "person" in d["raw"]
    assert d["_treg"]["upgrade"]["upgraded"] is False and d["_treg"]["upgrade"]["reason"] == "other_name"
    assert d["output"]["full_name"] == "Alex Example"


async def test_no_upgrade_when_the_caller_sent_that_key(clients, enrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"blitz": [(200, _example("blitzapi.people.enrich"))]}, seen))
    r = await clients.post(ENRICH, json=LINKEDIN, headers={"X-Treg-Route-Upgrade": "linkedin_url"})
    assert r.status_code == 200, r.text
    assert len(seen) == 1 and "upgrade" not in r.json()["_treg"]


async def test_an_upgrade_key_the_contract_cannot_take_alone_is_refused(clients, enrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({}, seen))
    before = await _balance(clients)
    r = await clients.post(ENRICH, json=EMAIL, headers={"X-Treg-Route-Upgrade": "domain"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_upgrade_unsupported"
    assert seen == [] and await _balance(clients) == before, "refused before anything is held"


def test_variant_slices_parse_and_unknown_names_do_not():
    snap = quality.parse({"slices": {"domain+full_name": {"a": {"decisive_rate": 0.5, "agreement": 0.9, "comparable": 300}},
                                     "Bad Slice": {"a": {"decisive_rate": 0.5, "agreement": 0.9, "comparable": 300}}}})
    assert list(snap) == ["domain+full_name"]
    assert quality.variant_slice(("full_name", "domain")) == "domain+full_name"
