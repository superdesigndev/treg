"""`/table/<tool id>`: the same call as `/call/`, answered as rows and columns.

Two halves: the pure converter (`domain.table`) on saved real answers, and the route end to end
(the flag, the four shapes, an upstream error, the money, and an `Idempotency-Key` replay).
Design: tools-gsheet `docs/decisions.md`; the mechanism: docs/context/architecture/table.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.test_marketplace_call import _fake_relay, platform_on  # noqa: F401 - tier 4 on
from treg.application import table as table_app
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.table import cell, to_table

EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "treg" / "catalog" / "examples"
EP = "tikhub.tiktok.video.comments"      # a real catalog GET endpoint with a platform price


def _example(name: str):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


# ------------------------------------------------------------------------------------------------
# The converter, on saved real answers

def test_a_flat_answer_is_one_row_of_its_fields():
    t = to_table(_example("bounceban.people.email.verify"))
    assert t["shape"] == "flat" and t["column_source"] == "generated" and len(t["rows"]) == 1
    row = dict(zip(t["columns"], t["rows"][0]))
    assert row["email"] == "dev@bounceban.com" and row["result"] == "deliverable" and row["score"] == 99


def test_one_list_is_one_row_per_item_with_nested_fields_as_dotted_columns():
    t = to_table(_example("apollo.people.search"))
    assert t["shape"] == "list" and t["path"] == "people" and len(t["rows"]) >= 2
    assert "organization.name" in t["columns"] and "first_name" in t["columns"]
    assert "_note" not in t["columns"] and "_scrubbed" not in t["columns"]   # `_` fields are never columns


def test_several_lists_are_nested_tables_and_a_summary_inside_a_one_item_wrapper():
    t = to_table(_example("seranking.web.backlinks.summary"))
    assert t["shape"] == "nested"
    names = {tb["name"]: tb for tb in t["tables"]}
    assert set(names) >= {"top_pages_by_backlinks", "top_tlds", "top_countries"}
    assert names["top_tlds"]["path"] == "summary[0].top_tlds" and names["top_tlds"]["columns"] == ["tld", "count"]
    summary = dict(t["summary"]["rows"])
    assert summary["backlinks"] == 259613 and summary["top_tlds"].startswith("com, info")
    assert t["columns"] == ["field", "value"] and t["rows"] == t["summary"]["rows"]   # a simple client still works


def test_a_routed_flat_job_uses_the_contract_order_then_served_by():
    body = {"output": {"score": 99, "valid": True, "status": "valid"}, "raw": {},
            "_treg": {"served_by": "trykitt.people.email.verify", "outcome": "hit"}}
    t = to_table(body, contract_output=["valid", "status", "score"])
    assert t["columns"] == ["valid", "status", "score", "served_by"]
    assert t["rows"] == [[True, "valid", 99, "trykitt.people.email.verify"]]
    assert t["column_source"] == "contract" and t["_treg"]["served_by"] == "trykitt.people.email.verify"


def test_a_routed_miss_has_no_rows():
    t = to_table({"output": {"valid": None}, "_treg": {"outcome": "miss", "served_by": None}},
                 contract_output=["valid", "status", "score"])
    assert t["rows"] == [] and t["_treg"]["outcome"] == "miss"


def test_a_routed_people_list_maps_common_names_first():
    people = _example("apollo.people.search")["people"]
    t = to_table({"output": {"people": people}, "_treg": {"served_by": "apollo.people.search"}},
                 contract_output=["people", "total"], list_field="people")
    assert t["shape"] == "list"
    assert t["columns"][:6] == ["first_name", "last_name", "title", "company", "linkedin_url", "location"]
    row = dict(zip(t["columns"], t["rows"][0]))
    assert row["first_name"] == people[0]["first_name"]
    assert row["last_name"] == people[0]["last_name_obfuscated"]              # a mapped provider name
    assert row["company"] == people[0]["organization"]["name"]                # a mapped dotted path
    assert "last_name_obfuscated" not in t["columns"]                          # used once, not twice


def test_a_hub_tool_is_its_manifest_fields_in_order():
    t = to_table({"run_id": "r1", "output": {"count": 2, "rows": [1, 2]}, "usage": {"cost_micro": 5}},
                 hub_fields=["rows", "count"])
    assert t["columns"] == ["rows", "count"] and t["rows"] == [["1, 2", 2]] and t["column_source"] == "hub"


def test_cells():
    assert cell(["a", "b", 3]) == "a, b, 3"                      # decision B: one cell
    assert cell({"a": 1}) == '{"a":1}' and cell([{"a": 1}]) == '[{"a":1}]'
    assert cell(None) is None and cell(1.5) == 1.5


def test_a_strange_body_never_raises():
    assert to_table(["x", 1, None])["shape"] == "list"
    assert to_table("text")["shape"] == "flat"


def test_the_contract_of_a_routed_job_is_found_in_the_catalog():
    fields, list_field = table_app._contract("treg.people.email.verify")
    assert fields[:3] == ["valid", "status", "score"] and list_field is None
    fields, list_field = table_app._contract("treg.people.search")
    assert list_field == "people"
    assert table_app._contract(EP) is None                        # not a routed job


# ------------------------------------------------------------------------------------------------
# The route, end to end

@pytest.fixture
def table_on(monkeypatch):
    monkeypatch.setenv("TREG_TABLE_ENABLED", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _money(clients: AsyncClient) -> tuple[int, list]:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    b = (await clients.get(f"/orgs/{org_id}/balance")).json()
    return b["balance_micro"], b.get("holds") or []


async def test_the_flag_off_is_a_404_that_leaves_no_trace(clients: AsyncClient, platform_on):
    r = await clients.get(f"/table/{EP}?aweme_id=1")
    assert r.status_code == 404 and "x-treg-error" not in r.headers
    calls = (await clients.get("/calls", params={"limit": 20})).json()
    assert not [c for c in calls if (c.get("path") or "").startswith("/table/")]


async def test_a_team_outside_the_lists_gets_the_same_404(clients: AsyncClient, platform_on, table_on, monkeypatch):
    monkeypatch.setenv("TREG_TABLE_USERS", "someone-else@example.com")
    get_settings.cache_clear()
    assert (await clients.get(f"/table/{EP}?aweme_id=1")).status_code == 404


async def test_each_shape_end_to_end_and_the_same_charge_as_call(clients: AsyncClient, platform_on, table_on, monkeypatch):
    cases = {
        "flat": (b'{"id": 7, "author": {"name": "a"}}', lambda t: t["columns"] == ["id", "author.name"]),
        "list": (b'{"data": [{"a": 1}, {"a": 2}], "total": 2}', lambda t: t["rows"] == [[1], [2]]),
        "nested": (b'{"x": [{"a": 1}, {"a": 2}], "y": [{"b": 1}, {"b": 2}], "n": 2}',
                   lambda t: [tb["name"] for tb in t["tables"]] == ["x", "y"]),
        "raw": (b"not json at all", lambda t: t["rows"] == [["not json at all"]]),
    }
    before, _ = await _money(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=0")
    assert r.status_code == 200
    after, _ = await _money(clients)
    one_call = before - after
    assert one_call > 0
    for i, (shape, (body, check)) in enumerate(cases.items(), start=1):
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, body))
        before, _ = await _money(clients)
        r = await clients.get(f"/table/{EP}?aweme_id={i}")
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["shape"] == shape and check(t), (shape, t)
        assert r.headers["x-treg-call-id"] == t["_treg"]["call_id"]
        after, holds = await _money(clients)
        assert before - after == one_call, shape                  # charged exactly as /call/ charges
        assert int(r.headers["x-treg-cost-micro"]) == one_call
        assert holds == []                                        # no hold left open


async def test_an_upstream_error_keeps_its_status_and_releases_the_hold(clients: AsyncClient, platform_on, table_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(502, b'{"message": "vendor down"}'))
    before, _ = await _money(clients)
    r = await clients.get(f"/table/{EP}?aweme_id=err")
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "upstream_error" and body["upstream_status"] == 502
    assert "vendor down" in body["body_excerpt"]
    after, holds = await _money(clients)
    assert after == before and holds == []                        # a vendor failure costs nothing


async def test_an_idempotency_key_replays_the_same_table_for_free(clients: AsyncClient, platform_on, table_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": [{"a": 1}, {"a": 2}]}'))
    h = {"Idempotency-Key": "sheet-1-row-7"}
    first = await clients.get(f"/table/{EP}?aweme_id=idem", headers=h)
    assert first.status_code == 200, first.text
    before, _ = await _money(clients)
    again = await clients.get(f"/table/{EP}?aweme_id=idem", headers=h)
    after, _ = await _money(clients)
    assert again.status_code == 200 and after == before
    assert again.headers.get("x-treg-idempotent-replay")
    assert again.json()["rows"] == first.json()["rows"] and again.json()["columns"] == first.json()["columns"]


async def test_a_refusal_is_marked_as_treg_s_own_error(clients: AsyncClient, table_on):
    r = await clients.get("/table/no-such-tool/x")
    assert r.status_code == 404 and r.headers.get("x-treg-error") == "1"


async def test_a_hub_tool_is_its_manifest_fields(clients: AsyncClient, platform_on, table_on, monkeypatch):
    from tests.test_hub import PER_ITEM, _publish_script_priced
    monkeypatch.setenv("TREG_HUB_ENABLED", "1")
    get_settings.cache_clear()
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=3)
    r = await clients.post(f"/table/{tool_id}", json={})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["column_source"] == "hub" and t["columns"] == ["rows", "count"]
    assert t["rows"] == [["1, 1, 1", 3]]


# ------------------------------------------------------------------------------------------------
# The free column preview: GET /table-columns/<tool id>

async def test_the_preview_shows_the_columns_for_free_and_leaves_no_trace(clients: AsyncClient, platform_on, table_on, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a preview must never call a provider")
    monkeypatch.setattr(call_service, "relay", boom)
    before, _ = await _money(clients)
    flat = (await clients.get("/table-columns/treg.people.email.verify")).json()
    coverage = flat.pop("coverage")
    assert flat == {"shape": "flat", "columns": ["valid", "status", "score", "served_by"], "column_source": "contract"}
    from treg.domain.catalog import store as catalog_store
    cat = catalog_store.load()
    kids = [k for k in cat.by_id["treg.people.email.verify"]["routed_children"] if k in cat.adapters]
    assert set(coverage) == {"valid", "status", "score"}
    assert coverage["valid"] == {"filled_by": len(kids), "providers": len(kids)}       # every provider
    assert coverage["score"]["filled_by"] == sum(1 for k in kids if "score" in cat.adapters[k].out_map)
    assert 0 < coverage["score"]["filled_by"] < len(kids)                              # only some
    people = (await clients.get("/table-columns/treg.people.search")).json()
    assert people["shape"] == "list" and people["column_source"] == "contract"
    assert people["columns"][:6] == ["first_name", "last_name", "title", "company", "linkedin_url", "location"]
    assert len(people["columns"]) > 6                               # + a saved example's own fields
    assert set(people["coverage"]) == {"people"}                   # a list job: the list field only
    nested = (await clients.get("/table-columns/seranking.web.backlinks.summary")).json()
    assert nested["shape"] == "nested" and nested["column_source"] == "generated"
    assert {"name": "top_tlds", "path": "summary[0].top_tlds", "row_count": 2, "columns": ["tld", "count"]} in nested["tables"]
    assert "rows" not in nested and all("rows" not in tb for tb in nested["tables"])
    assert "coverage" not in nested                                # not a routed tool
    generated = (await clients.get("/table-columns/bounceban.people.email.verify")).json()
    assert generated["shape"] == "flat" and "email" in generated["columns"]
    missing = await clients.get("/table-columns/no-such.tool")
    assert missing.status_code == 404 and missing.json()["error"] == "no_preview"
    after, _ = await _money(clients)
    assert after == before
    calls = (await clients.get("/calls", params={"limit": 20})).json()
    assert not [c for c in calls if (c.get("path") or "").startswith("/table")]


async def test_the_preview_follows_the_flag(clients: AsyncClient):
    assert (await clients.get("/table-columns/treg.people.email.verify")).status_code == 404


async def test_the_preview_of_a_hub_tool_is_its_manifest_fields(clients: AsyncClient, platform_on, table_on, monkeypatch):
    from tests.test_hub import PER_ITEM, _publish_script_priced
    monkeypatch.setenv("TREG_HUB_ENABLED", "1")
    get_settings.cache_clear()
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=3)
    r = await clients.get(f"/table-columns/{tool_id}")
    assert r.json() == {"shape": "flat", "columns": ["rows", "count"], "column_source": "hub"}


@pytest.mark.parametrize("provider, expect", [
    # the provider's own field names, mapped to the fixed columns (decision F)
    ("quickenrich.people.search.domain", {"linkedin_url": "employee_linkedin", "company": "company_name"}),
    ("aiark.people.search", {"first_name": "profile.first_name", "title": "profile.title",
                             "company": "company.name", "linkedin_url": "link.linkedin"}),
    ("lusha.people.search", {"title": "jobTitle.title", "company": "company.name",
                             "linkedin_url": "socialLinks.linkedin", "location": "location.city"}),
    ("wiza.people.search", {"title": "job_title", "company": "job_company_name", "location": "location_name"}),
    ("crustdata.people.search", {"title": "basic_profile.current_title",
                                 "location": "basic_profile.location.full_location"}),
    ("icypeas.people.search", {"title": "lastJobTitle", "company": "lastCompanyName"}),
    ("dropleads.people.search", {"company": "organization_name", "location": "city"}),
    ("aviato.people.search", {"linkedin_url": "URLs.linkedin"}),
])
def test_each_people_provider_fills_the_fixed_columns(provider, expect):
    from treg.domain.catalog import store as catalog_store
    from treg.domain.table import flatten, sample_items
    items = sample_items(table_app._example_body(catalog_store.load().by_id[provider]))
    t = to_table({"output": {"people": items}, "_treg": {}}, contract_output=["people"], list_field="people")
    row = dict(zip(t["columns"], t["rows"][0]))
    source = flatten(items[0])
    for column, path in expect.items():
        assert row[column] == source[path], (provider, column, path)
        assert path not in t["columns"], (provider, path)              # used once, not twice
    for column in ("first_name", "last_name", "title", "company", "linkedin_url", "location"):
        assert not (isinstance(row[column], str) and row[column].startswith("{")), (provider, column)


@pytest.mark.parametrize("provider, expect", [
    ("prospeo.companies.search", {"name": "company.name", "domain": "company.domain",
                                  "employees": "company.employee_count", "location": "company.location.city"}),
    ("crustdata.companies.search", {"name": "basic_info.name", "domain": "basic_info.primary_domain"}),
    ("leadmagic.x.companies-search-v3", {"name": "company_name", "domain": "company_domain",
                                          "industry": "company_industry_linkedin", "location": "hq_city"}),
    ("thecompaniesapi.companies.search", {"name": "about.name", "domain": "domain.domain"}),
    ("aviato.companies.search", {"domain": "URLs.website", "linkedin_url": "URLs.linkedin"}),
    ("exa.companies.search", {"name": "title", "domain": "url"}),
    ("contactout.companies.search", {"domain": "domain"}),            # its `url` is the LinkedIn page
])
def test_each_company_provider_fills_the_fixed_columns(provider, expect):
    from treg.domain.catalog import store as catalog_store
    from treg.domain.table import flatten, sample_items
    items = sample_items(table_app._example_body(catalog_store.load().by_id[provider]))
    t = to_table({"output": {"companies": items}, "_treg": {}}, contract_output=["companies"],
                 list_field="companies")
    assert t["columns"][:6] == ["name", "domain", "industry", "employees", "location", "linkedin_url"]
    row = dict(zip(t["columns"], t["rows"][0]))
    source = flatten(items[0])
    for column, path in expect.items():
        assert row[column] == source[path], (provider, column, path)


def test_a_path_reads_into_a_list_by_index():
    # exa keeps a company's facts in `entities[0].properties`
    item = {"title": "B2B Rocket", "url": "https://b2brocket.ai/", "entities": [{"properties": {
        "workforce": {"total": 150}, "headquarters": {"address": "Lewes, DE 19958, US"}}}]}
    t = to_table({"output": {"companies": [item]}, "_treg": {}}, contract_output=["companies"], list_field="companies")
    row = dict(zip(t["columns"], t["rows"][0]))
    assert (row["employees"], row["location"]) == (150, "Lewes, DE 19958, US")


def test_a_one_item_list_is_one_row_unless_it_wraps_tables():
    t = to_table({"data": [{"name": "a", "email": "a@x.com"}], "total": 1})
    assert t["shape"] == "list" and t["columns"] == ["name", "email"] and t["rows"] == [["a", "a@x.com"]]
    wrapped = to_table({"tasks": [{"result": [{"k": 1}, {"k": 2}]}]})
    assert wrapped["shape"] == "list" and wrapped["path"] == "tasks[0].result"

