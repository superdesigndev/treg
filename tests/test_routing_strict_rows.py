"""`X-Treg-Route-Strict-Filters` judges rows: a row whose own field breaks a sent filter leaves
`output`, `raw` keeps the provider's body whole, and every answered child is billed as usual."""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import row_filters as R
from treg.domain.catalog.routing.contracts import parse_contracts

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _relay_by_provider

TODAY = date(2026, 11, 28)


def _apply(key, test, fields, sent, rows):
    return R.apply((R.RowFilter(key, test, tuple(fields)),), {key: sent}, {key}, rows, TODAY)


def test_place_keeps_rows_in_the_place_and_removes_the_rest():
    rows = [{"location": "Austin, TX, US"}, {"location": "Dallas, Texas, United States"},
            {"location": "Greater Austin Area"}, {"location": "Berlin, Germany"}, {"name": "no place"}]
    r = _apply("location", "place", ["location"], "Austin, Texas", rows)
    assert [x.get("location") for x in r.rows] == ["Austin, TX, US", "Greater Austin Area", None]
    assert r.removed == 2 and r.unchecked == 1 and r.by == {"location": 2}
    # a country or US state matches its code in a row ('Paris, FR', 'Portland, OR')
    assert R.place("France", "Paris, FR") and R.place("Oregon, United States", "Portland, OR")
    assert R.place("United Kingdom", "Manchester, England") and R.place("Greater Phoenix", "Phoenix, Arizona")
    assert R.place("London, United Kingdom", "Manchester, England") is False


def test_domain_keeps_the_company_and_its_subdomains():
    rows = [{"company_domain": "stripe.com"}, {"company_domain": "jobs.stripe.com"},
            {"company_domain": "notstripe.com"}, {"company_domain": "acme.io"}]
    r = _apply("company_domain", "domain", ["company_domain"], "https://www.stripe.com", rows)
    assert [x["company_domain"] for x in r.rows] == ["stripe.com", "jobs.stripe.com"] and r.removed == 2


def test_site_keeps_rows_on_the_questions_site():
    rows = [{"link": "https://www.a.com/blog/post"}, {"url": "https://a.com/shop"}, {"url": "https://b.com/blog"}]
    r = _apply("q", "site", ["url", "link"], "pricing site:a.com/blog", rows)
    assert r.rows == rows[:1] and r.removed == 2
    # a question without `site:` is not judged at all
    assert _apply("q", "site", ["url", "link"], "pricing", rows) is None


def test_within_days_keeps_rows_posted_in_the_window():
    rows = [{"date_posted": "2026-11-25T10:00:00+00:00"}, {"date_first_seen": "2026-11-21"},
            {"date_posted": "2026-10-01"}, {"date_posted": "last week"}]
    r = _apply("posted_within_days", "within_days", ["date_posted", "date_first_seen"], 7, rows)
    assert r.rows == [rows[0], rows[1], rows[3]] and r.removed == 1 and r.unchecked == 1


def test_a_filter_the_caller_did_not_send_judges_nothing():
    f = (R.RowFilter("location", "place", ("location",)),)
    assert R.apply(f, {"location": "Austin"}, set(), [{"location": "Berlin"}], TODAY) is None


def test_row_filters_refuse_a_key_or_field_the_contract_does_not_have():
    base = {"identity": [{"q": "string"}], "filters": {"location": {"type": "string"}},
            "output": {"people": {"type": "array", "required": True}}, "miss": "people == []",
            "row": {"of": "people", "needs": ["name"], "fields": {"name": {"type": "string"}, "location": {"type": "string"}}}}
    ok = parse_contracts({"contracts": {"x": {**base, "row_filters": {"location": {"test": "place", "field": "location"}}}}})
    assert ok["x"].row_filters == (R.RowFilter("location", "place", ("location",)),)
    for bad in ({"country": {"test": "place", "field": "location"}}, {"location": {"test": "near", "field": "location"}},
                {"location": {"test": "place", "field": "city"}}):
        with pytest.raises(ValueError, match="row_filters"):
            parse_contracts({"contracts": {"x": {**base, "row_filters": bad}}})


def test_shipped_contracts_declare_their_row_filters():
    contracts = catalog_store.load().contracts
    assert {f.key for f in contracts["people.search"].row_filters} == {"location", "company_domain"}
    assert {f.key for f in contracts["companies.jobs.search"].row_filters} == {"location", "posted_within_days"}
    assert {f.key for f in contracts["web.search"].row_filters} == {"q"}


# ---- through the routed call ------------------------------------------------------------------

LEADS = {"leads": [
    {"firstname": "Ann", "lastname": "A", "lastCompanyWebsite": "https://stripe.com", "address": "Dublin, Ireland", "profileUrl": "https://www.linkedin.com/in/ann"},
    {"firstname": "Bob", "lastname": "B", "lastCompanyWebsite": "https://acme.io", "address": "Dublin, Ireland", "profileUrl": "https://www.linkedin.com/in/bob"},
    {"firstname": "Cat", "lastname": "C", "lastCompanyWebsite": "https://stripe.com", "address": "London, United Kingdom", "profileUrl": "https://www.linkedin.com/in/cat"},
    {"firstname": "Dan", "lastname": "D", "lastCompanyWebsite": "https://stripe.com", "profileUrl": "https://www.linkedin.com/in/dan"},
], "total": 4}
FAR = {"leads": [{"firstname": "Eve", "lastname": "E", "lastCompanyWebsite": "https://acme.io", "address": "Berlin, Germany",
                  "profileUrl": "https://www.linkedin.com/in/eve"}], "total": 1}
ASK = {"company_domain": "stripe.com", "title": "Engineer", "location": "Dublin, Ireland", "limit": 4}


@pytest.fixture
def icypeas_only(monkeypatch, platform_on):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas")
    get_settings.cache_clear()
    cat = catalog_store.load()
    for eid in cat.by_id["treg.people.search"]["routed_children"]:
        if eid != "icypeas.people.search":
            monkeypatch.delitem(cat.adapters, eid)
    yield
    get_settings.cache_clear()


async def _search(clients, monkeypatch, answer, *, strict: bool):
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, answer)]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json=ASK,
                           headers={"X-Treg-Route-Strict-Filters": "1"} if strict else {})
    assert r.status_code == 200 and len(seen) == 1, r.text
    return r, before - await _balance(clients)


async def test_strict_filters_remove_rows_off_place_or_off_company_and_bill_as_usual(clients: AsyncClient, icypeas_only, monkeypatch):
    plain, plain_cost = await _search(clients, monkeypatch, LEADS, strict=False)
    strict, strict_cost = await _search(clients, monkeypatch, LEADS, strict=True)
    # a call without the header is unchanged: every row, no note
    assert [p["name"] for p in plain.json()["output"]["people"]] == ["Ann A", "Bob B", "Cat C", "Dan D"]
    assert "strict_filtered" not in plain.json()["_treg"] and "X-Treg-Strict-Filtered" not in plain.headers
    body = strict.json()
    # Bob works elsewhere, Cat is in London; Dan names no place, so he is kept, unchecked
    assert [p["name"] for p in body["output"]["people"]] == ["Ann A", "Dan D"]
    assert body["_treg"]["strict_filtered"] == {"removed": 2, "kept": 2, "by": {"company_domain": 1, "location": 1},
                                               "unchecked": 1, "in_raw": True}
    assert strict.headers["X-Treg-Strict-Filtered"] == "removed=2,kept=2"
    assert body["raw"] == LEADS and body["_treg"]["outcome"] == "hit"   # the child's answer, whole
    assert strict_cost == plain_cost > 0 and body["_treg"]["charged_micro"] == plain.json()["_treg"]["charged_micro"]


async def test_strict_filters_that_remove_every_row_answer_a_billed_miss_with_raw_whole(clients: AsyncClient, icypeas_only, monkeypatch):
    plain, plain_cost = await _search(clients, monkeypatch, FAR, strict=False)
    strict, strict_cost = await _search(clients, monkeypatch, FAR, strict=True)
    body = strict.json()
    assert body["output"]["people"] == [] and body["raw"] == FAR
    assert body["_treg"]["outcome"] == "miss" and strict.headers["X-Treg-Route-Outcome"] == "miss"
    assert body["_treg"]["strict_filtered"]["removed"] == 1 and body["_treg"]["strict_filtered"]["kept"] == 0
    assert "strict filters removed every row" in body["_treg"]["tried"][0]["detail"]
    # the provider answered: its call settles at its own price, as without the header
    assert strict_cost == plain_cost > 0 and body["_treg"]["charged_micro"] == plain.json()["_treg"]["charged_micro"]
