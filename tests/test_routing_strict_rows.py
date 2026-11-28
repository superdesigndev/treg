"""`X-Treg-Route-Strict-Filters` judges rows: a row whose own field breaks a sent filter leaves
`output`, `raw` keeps the provider's body whole, and every answered child is billed as usual."""

from __future__ import annotations

import json
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


# (asked, row, verdict): True keeps it as a match, None keeps it unchecked, False removes it.
PLACES = [
    # a country, a state or a metro asked: a row inside it is never removed
    ("United States", "Austin, TX", True),
    ("United States", "Austin, Texas, United States", True),
    ("Germany", "Berlin", None),
    ("Germany", "Berlin, Germany", True),
    ("California", "Greater Los Angeles Area", None),
    ("California", "San Francisco, CA", None),          # CA is also Canada: never a match, never removed
    ("California", "San Francisco, CA, US", True),
    ("California", "San Francisco, California, United States", True),
    ("New York City", "New York, New York, United States", True),
    ("New York City", "New York City Metropolitan Area", True),
    ("New York", "Brooklyn, NY", True),
    ("Greater Phoenix", "Scottsdale, Arizona", None),
    ("Greater Phoenix", "Phoenix, Arizona, United States", True),
    ("Texas", "Texas", True),
    ("Austin, Texas", "Texas, United States", None),        # only a larger place: not judged
    ("Austin, Texas", "United States", None),
    ("Indiana", "Columbus, IN", None),                       # IN is also India
    ("Indiana", "Columbus, IN, US", True),
    ("Germany", "Berlin, DE", None),                         # DE is also Delaware
    ("United States", "Columbus, IN", None),
    ("United Kingdom", "Austin, MN", False),
    # the city cases
    ("Paris, France", "Paris, FR", True),
    ("France", "Paris, FR", True),
    ("Oregon, United States", "Portland, OR", True),
    ("Austin, Texas", "Austin, TX, US", True),
    ("Austin, Texas", "Greater Austin Area", True),
    ("Zürich, Switzerland", "Zurich, Switzerland", True),
    ("São Paulo", "Sao Paulo, SP, Brazil", True),
    ("London, United Kingdom", "Greater London, England, United Kingdom", True),
    # a different place at the same level: removed
    ("United States", "Berlin, Germany", False),
    ("Germany", "Austin, TX, US", False),
    ("California", "Austin, Texas, United States", False),
    ("California", "Austin, TX", False),
    ("Austin, Texas", "Dallas, Texas, United States", False),
    ("Austin, Texas", "Austin, MN", False),
    ("London, United Kingdom", "Manchester, England", None),  # outside the US only the country is judged
    ("Paris, France", "Lyon, France", None),
    # a city with nothing around it says nothing about a row elsewhere
    ("London", "Berlin, Germany", None),
    ("Greater Phoenix", "Berlin, Germany", None),
]


@pytest.mark.parametrize("asked,row,verdict", PLACES)
def test_place_removes_only_a_row_that_names_another_place_at_the_level_asked(asked, row, verdict):
    assert R.place(asked, row) is verdict


# A second table: "keep" may be a match or unchecked, "remove" must be removed.
KEEP_OR_REMOVE = [
    ("Austin, TX", "Paris, France", "remove"),
    ("Berlin", "Toronto, Canada", "remove"),
    ("Berlin, Germany", "Toronto, Ontario, Canada", "remove"),
    ("Austin, Texas", "Austin, TX, United States", "keep"),
    ("Austin, TX", "Dallas, TX", "remove"),
    ("Austin, TX", "Texas, United States", "keep"),
    ("Austin, TX", "Greater Austin Area", "keep"),
    ("Austin, TX", "Austin, Texas Metropolitan Area", "keep"),
    ("Texas", "Houston, TX", "keep"),
    ("Texas", "Sacramento, CA", "remove"),
    ("California", "San Francisco Bay Area", "keep"),
    ("California", "Los Angeles, California, United States", "keep"),
    ("CA", "San Jose, CA", "keep"),
    ("California", "Toronto, ON, Canada", "remove"),
    ("Canada", "Toronto, Ontario, Canada", "keep"),
    ("Canada", "Vancouver, BC", "keep"),
    ("Canada", "Seattle, WA", "remove"),
    ("Indiana", "Indianapolis, IN", "keep"),
    ("India", "Bengaluru, Karnataka, India", "keep"),
    ("India", "Indianapolis, IN", "remove"),
    ("IN", "Mumbai, India", "keep"),
    ("Georgia", "Atlanta, GA", "keep"),
    ("Georgia", "Tbilisi, Georgia", "keep"),
    ("Atlanta, GA", "Tbilisi, Georgia", "remove"),
    ("Atlanta, Georgia", "Atlanta, GA, United States", "keep"),
    ("San Francisco, CA", "San Francisco Bay Area", "keep"),
    ("San Francisco", "Bay Area", "keep"),
    ("London", "Greater London", "keep"),
    ("London, UK", "London, England, United Kingdom", "keep"),
    ("London, United Kingdom", "London, Ontario, Canada", "remove"),
    ("London", "Manchester, England", "keep"),
    ("United Kingdom", "Greater London", "keep"),
    ("United Kingdom", "Paris, Île-de-France, France", "remove"),
    ("Dallas, TX", "DFW", "keep"),
    ("Dallas, TX", "Dallas-Fort Worth Metroplex", "keep"),
    ("Texas", "DFW", "keep"),
    ("Munich", "München, Bayern, Deutschland", "keep"),
    ("München", "Munich, Bavaria, Germany", "keep"),
    ("Munich, Germany", "München, Germany", "keep"),
    ("Cologne", "Köln, Germany", "keep"),
    ("Köln, Germany", "Cologne, North Rhine-Westphalia, Germany", "keep"),
    ("Germany", "Köln, Nordrhein-Westfalen, Deutschland", "keep"),
    ("Germany", "Berlin, Germany", "keep"),
    ("Germany", "Vienna, Austria", "remove"),
    ("New York", "Brooklyn, NY", "keep"),
    ("New York, NY", "New York City Metropolitan Area", "keep"),
    ("New York, NY", "Jersey City, NJ", "remove"),
    ("New York", "Remote", "keep"),
    ("Austin, TX", "Remote - US", "keep"),
    ("United States", "Remote", "keep"),
    ("United States", "Toronto, Canada", "remove"),
    ("US", "Austin, TX", "keep"),
    ("USA", "London, UK", "remove"),
    ("Paris", "Paris, TX", "keep"),
    ("Paris, France", "Paris, TX", "remove"),
    ("Portland, OR", "Portland, ME", "remove"),
    ("Washington", "Seattle, WA", "keep"),
    ("Washington, DC", "Washington, District of Columbia", "keep"),
    ("Ontario, Canada", "Toronto, ON", "keep"),
    ("São Paulo, Brazil", "Sao Paulo, SP, Brasil", "keep"),
    ("Zurich, Switzerland", "Zürich, Schweiz", "keep"),
    ("Remote", "Austin, TX", "keep"),
    ("Minnesota", "Minneapolis, MN", "keep"),
    ("Mongolia", "Minneapolis, MN", "remove"),
    ("California", "Hamburg, Germany", "remove"),
]
# Clear to a reader, not to the test: a city asked with nothing around it, and a row whose
# two-letter code is a US state and also the country asked.
NOT_JUDGED = {("Berlin", "Toronto, Canada"), ("India", "Indianapolis, IN"), ("Mongolia", "Minneapolis, MN")}


@pytest.mark.parametrize("asked,row,want", KEEP_OR_REMOVE)
def test_place_never_removes_a_row_in_the_place_asked(asked, row, want):
    got = R.place(asked, row)
    if (asked, row) in NOT_JUDGED:
        assert got is None
    else:
        assert (got is False) == (want == "remove"), got


@pytest.mark.parametrize("name", ["Washington, DC", "Kansas City, MO", "Nevada City, CA", "Indiana, PA",
                                  "Delaware, OH", "Michigan City, IN", "New York City", "Texas"])
def test_a_place_always_matches_itself(name):
    assert R.place(name, name) is True


def test_a_code_after_a_city_in_the_ask_is_the_us_state():
    assert R.place("San Francisco, CA", "Paris, France") is False
    assert R.place("San Francisco, CA", "Los Angeles, CA") is False
    assert R.place("San Francisco, CA", "San Francisco, CA, US") is True


def test_place_counts_rows_removed_and_unchecked():
    rows = [{"location": "Austin, TX, US"}, {"location": "Dallas, Texas, United States"},
            {"location": "Greater Austin Area"}, {"location": "Berlin, Germany"}, {"location": "Texas"},
            {"name": "no place"}]
    r = _apply("location", "place", ["location"], "Austin, Texas", rows)
    assert [x.get("location") for x in r.rows] == ["Austin, TX, US", "Greater Austin Area", "Texas", None]
    assert r.removed == 2 and r.unchecked == 2 and r.by == {"location": 2}


def test_domain_keeps_the_company_and_its_subdomains():
    rows = [{"company_domain": "stripe.com"}, {"company_domain": "jobs.stripe.com"}, {"company_domain": "stripe.com."},
            {"company_domain": "user@stripe.com"},
            {"company_domain": "stripe.com:443"}, {"company_domain": "notstripe.com"}, {"company_domain": "acme.io"}]
    r = _apply("company_domain", "domain", ["company_domain"], "https://www.stripe.com", rows)
    assert [x["company_domain"] for x in r.rows] == ["stripe.com", "jobs.stripe.com", "stripe.com.", "user@stripe.com",
                                                     "stripe.com:443"]
    assert r.removed == 2


def test_site_keeps_rows_on_the_questions_site():
    rows = [{"link": "https://www.a.com/blog/post"}, {"url": "https://a.com/shop"}, {"url": "https://b.com/blog"}]
    r = _apply("q", "site", ["url", "link"], "pricing site:a.com/blog", rows)
    assert r.rows == rows[:1] and r.removed == 2
    # a question without `site:` is not judged at all
    assert _apply("q", "site", ["url", "link"], "pricing", rows) is None


@pytest.mark.parametrize("q,link", [
    ("x site:.edu", "https://mit.edu/a"), ("x site:*.a.com", "https://b.com/a"), ("x site:edu", "https://b.com"),
    ("x site:a.com", "https://www.google.com/url?q=https://a.com/p"), ("x site:a.com/caf%C3%A9", "https://a.com/x"),
    ("x site:a.com", "https://b.com/%2Fa.com"), ("x site:a.com", "not a link"),
])
def test_site_does_not_judge_what_it_cannot_read(q, link):
    assert R.site(q, link) is None


def test_within_days_keeps_rows_posted_in_the_window():
    rows = [{"date_posted": "2026-11-25T10:00:00+00:00"}, {"date_first_seen": "2026-11-21"},
            {"date_posted": "2026-10-01"}, {"date_posted": "last week"}]
    r = _apply("posted_within_days", "within_days", ["date_posted", "date_first_seen"], 7, rows)
    assert r.rows == [rows[0], rows[1], rows[3]] and r.removed == 1 and r.unchecked == 1


def test_within_days_reads_the_row_date_in_utc():
    # 20 November 22:00 at UTC-5 is 21 November in UTC: inside a 7-day window from 28 November
    assert R.within_days(7, "2026-11-20T22:00:00-05:00", TODAY) is True
    assert R.within_days(7, "2026-11-21T02:00:00+05:00", TODAY) is False
    assert R.within_days(7, "2026-11-21T00:00:00Z", TODAY) is True


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


def test_the_loader_refuses_a_field_no_providers_row_carries():
    from dataclasses import replace

    from treg.domain.catalog.routing.contracts import row_filter_fields_unread
    cat = catalog_store.load()

    def example(ep):
        path = catalog_store.example_path(ep["id"])
        return json.loads(path.read_text()) if path else ep.get("example_response")

    assert row_filter_fields_unread(cat.contracts, cat.adapters, cat.by_id, example) == []
    jobs = cat.contracts["companies.jobs.search"]
    wrong = {**cat.contracts, "companies.jobs.search": replace(
        jobs, row_filters=(R.RowFilter("posted_within_days", "within_days", ("posted_on",)),))}
    assert row_filter_fields_unread(wrong, cat.adapters, cat.by_id, example) == [
        "companies.jobs.search.posted_within_days: posted_on"]


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


OFF_COMPANY = {
    "leads": [{"firstname": "Eve", "lastname": "E", "firstName": "Eve", "lastName": "E", "lastCompanyWebsite": "https://acme.io",
               "company": {"domain": "acme.io"}, "profileUrl": "https://www.linkedin.com/in/eve"}],
    "people": [{"contact_full_name": "Eve E", "company_domain": "acme.io"}],
    "data": [{"first_name": "Eve", "last_name": "E", "company_url": "https://acme.io"}],
    "contacts": [{"name": "Eve E", "domain": "acme.io", "email": "eve@acme.io"}],
    "total": 1, "count": 1,
}
COMPANY_SEARCHES = ("icypeas.people.search", "leadsforge.people.search", "leadmagic.people.search",
                    "quickenrich.people.search.domain", "findymail.search.domain")


@pytest.fixture
def five_company_searches(monkeypatch, platform_on):
    for p in ("ICYPEAS", "LEADSFORGE", "LEADMAGIC", "QUICKENRICH", "FINDYMAIL"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas,leadsforge,leadmagic,quickenrich,findymail")
    get_settings.cache_clear()
    cat = catalog_store.load()
    for eid in cat.by_id["treg.people.search"]["routed_children"]:
        if eid not in COMPANY_SEARCHES:
            monkeypatch.delitem(cat.adapters, eid)
    yield
    get_settings.cache_clear()


async def test_strict_filters_ask_at_most_two_more_providers_after_an_emptied_answer(
        clients: AsyncClient, five_company_searches, monkeypatch):
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, OFF_COMPANY)] * 5}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json={"company_domain": "stripe.com", "limit": 1},
                           headers={"X-Treg-Route-Strict-Filters": "1"})
    body = r.json()
    assert r.status_code == 200 and body["_treg"]["outcome"] == "miss", r.text
    asked = [t for t in body["_treg"]["tried"] if t["outcome"] != "skipped"]
    # the first answer and at most two more, each billed at its own price
    assert len(seen) == 3 and len(asked) == 3, body["_treg"]["tried"]
    assert all("strict filters removed every row" in t["detail"] for t in asked)
    assert body["_treg"]["charged_micro"] == sum(t["charged_micro"] for t in asked) == before - await _balance(clients)
    assert body["output"]["people"] == [] and body["raw"] == OFF_COMPANY
    # without the header the first provider's rows are the answer
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, OFF_COMPANY)]}, seen))
    r = await clients.post("/call/treg.people.search", json={"company_domain": "stripe.com", "limit": 1})
    assert r.status_code == 200 and len(seen) == 1 and r.json()["_treg"]["outcome"] == "hit", r.text


async def test_weak_and_emptied_answers_share_one_bound(clients: AsyncClient, five_company_searches, monkeypatch):
    on = json.loads(json.dumps(OFF_COMPANY).replace("acme.io", "stripe.com"))
    seen: list = []
    # one row each where two make an answer (weak), alternating with answers strict filtering empties
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"*": [(200, on), (200, OFF_COMPANY), (200, on), (200, OFF_COMPANY), (200, on)]}, seen))
    r = await clients.post("/call/treg.people.search", json={"company_domain": "stripe.com", "limit": 2},
                           headers={"X-Treg-Route-Strict-Filters": "1", "X-Treg-Route-Min-Results": "2"})
    body = r.json()
    assert r.status_code == 200 and len(seen) == 3, body["_treg"]["tried"]
    assert body["_treg"]["outcome"] == "weak" and len(body["output"]["people"]) == 1


async def test_a_strict_emptied_next_page_still_carries_the_cursor(clients: AsyncClient, five_company_searches, monkeypatch):
    page1 = {**json.loads(json.dumps(OFF_COMPANY).replace("acme.io", "stripe.com")), "cursor": "p2"}
    page2 = {**OFF_COMPANY, "cursor": "p3"}
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, page1), (200, page2)]}, seen))
    ask = {"company_domain": "stripe.com", "limit": 1}
    headers = {"X-Treg-Route-Strict-Filters": "1", "X-Treg-Route-Prefer": "leadsforge"}
    first = await clients.post("/call/treg.people.search", json=ask, headers=headers)
    assert first.status_code == 200 and first.json()["_treg"]["served_by"] == "leadsforge.people.search", first.text
    cursor = first.json()["output"]["next_cursor"]
    assert cursor
    r = await clients.post("/call/treg.people.search", json={**ask, "cursor": cursor}, headers=headers)
    body = r.json()
    # every row of page 2 is at another company: the page is a miss, and the list still goes on
    assert r.status_code == 200 and body["_treg"]["outcome"] == "miss" and body["output"]["people"] == [], r.text
    assert body["output"]["next_cursor"] and body["output"]["next_cursor"] != cursor
    assert len(seen) == 2 and seen[1][2].get("cursor") == "p2"
