"""The rewrite to the v2 spelling: the section 4.4 table, one row at a time, and its file edit."""

from __future__ import annotations

import pytest
import yaml

from scripts import catalog_v2 as codemod
from treg.domain.catalog.schema import V2Row


@pytest.mark.parametrize("today,v2", [
    ("a.0.b", "response.a.0.b"), ("a[0].b", "response.a.0.b"), (".", "response"),
    ("[0].name", "response.0.name"), ("tasks.*.id", "response.tasks.*.id"),
    ("details[modality=IMAGE]", "response.details[modality=IMAGE]"),
])
def test_an_answer_path_moves_under_response(today, v2):
    assert codemod.response_path(today) == v2


@pytest.mark.parametrize("today,v2", [("body.x", "body.x"), ("queryParams.x", "query.x"), ("pathParams.x", "query.x")])
def test_a_request_path_names_its_location(today, v2):
    assert codemod.request_path(today) == v2


def test_a_header_is_never_request_evidence():
    with pytest.raises(codemod.Refused):
        codemod.request_path("headers.x")


@pytest.mark.parametrize("expr,predicate,widens", [
    ("x == null", {"path": "response.x", "op": "absent"}, False),
    ("coalesce(results, []) == []", {"any": [{"path": "response.results", "op": "absent"},
                                             {"path": "response.results", "op": "empty"}]}, False),
    ("status == 'NOT_FOUND'", {"path": "response.status", "op": "equals", "value": "NOT_FOUND"}, False),
    ("data[0].found == false", {"path": "response.data.0.found", "op": "equals", "value": False}, False),
    ("code == 404", {"path": "response.code", "op": "equals", "value": 404}, False),
    (". == []", {"path": "response", "op": "empty"}, True),
    ("contacts == []", {"path": "response.contacts", "op": "empty"}, True),
])
def test_an_adapter_expression_becomes_a_predicate(expr, predicate, widens):
    warnings: list[str] = []
    assert codemod.predicate(expr, warnings) == predicate
    assert bool(warnings) is widens


@pytest.mark.parametrize("expr", ["x != 1", "starts_with(reason, 'No')", "len(items) == 0", "a == b"])
def test_an_expression_with_no_zero_diff_predicate_is_refused(expr):
    with pytest.raises(codemod.Refused):
        codemod.predicate(expr, [])


PROVENANCE = {"source": "docs", "source_url": "https://example.com", "checked": "2026-10-01",
              "confidence": "documented"}


def _v2(change: codemod.Rewrite) -> V2Row:
    return V2Row.model_validate({"id": change.endpoint_id, **change.keys})


def test_a_counted_row_rewrites_its_hold_rounding_and_report():
    change = codemod.rewrite({"id": "demo.x", "cost": {
        "type": "per_result", "value": 1, "currency": "credit", "per": 10, "unit": "result",
        "reserve": {"rows": {"param": "body.limit", "default": {"value": 100, "source": "docs"}, "max": 10000}},
        "reported_charge": {"path": "credits_used", "unit": "credit"}, **PROVENANCE}}, provider="demo",
        check_branches=False)
    assert change.keys == {
        "price": {"value": "1", "currency": "credit", "per": 10, "round": "up", "unit": "result"},
        "provenance": PROVENANCE,
        "reserve": {"rows": {"param": "body.limit", "default": {"value": 100, "source": "docs"}, "max": 10000}},
        "settle": {"reported": {"path": "response.credits_used", "unit": "credit"}},
        "bill": {"miss": "charged", "error": "charged"}}
    assert change.replaced == ("cost",) and _v2(change)


def test_a_table_row_with_expect_and_an_adapter_miss_freezes_its_outcome():
    change = codemod.rewrite({"id": "demo.x", "expect": {"json_path": "base.code", "equals": 0}, "cost": {
        "type": "per_success", "currency": "USD", "settle": "table",
        "table": [{"when": {"body.model": "m1", "queryParams.size": 2}, "value": 0.28}],
        "fallback": {"value": 0.5, "note": "ceiling"}, **PROVENANCE}}, provider="demo",
        adapter={"miss": "items == null", "verified": True}, check_branches=False)
    assert change.keys["price"]["table"] == [{"when": [
        {"path": "body.model", "op": "equals", "value": "m1"},
        {"path": "query.size", "op": "equals", "value": 2}], "value": "0.28"}]
    assert change.keys["expect"] == {"path": "response.base.code", "op": "equals", "value": 0}
    assert change.keys["miss"] == {"when": {"path": "response.items", "op": "absent"}}
    assert change.keys["error"] == {"when": codemod.ENVELOPE} and change.keys["bill"] == {"error": "charged"}
    assert change.adapter_miss and change.replaced == ("cost", "expect") and _v2(change)


@pytest.mark.parametrize("spec", [{"type": "string"}, {"type": "array[string]"}])
def test_an_entity_hold_counts_the_one_declared_entity_field(spec):
    change = codemod.rewrite({"id": "demo.x", "input": {"body": {"domain": spec, "roles": {"type": "string"}}},
                              "cost": {"type": "per_result", "value": 1, "reserve": {"entities": {}},
                                       **PROVENANCE}}, provider="demo", check_branches=False)
    assert change.keys["reserve"] == {"entities": {"from": "body.domain"}} and _v2(change)


@pytest.mark.parametrize("row,reason", [
    ({"cost": {"type": "quota_rows", "value": 1}}, "quota_rows"),
    ({"cost": {"type": "per_success", "value": 1, "settle": "modifiers"}}, "plugin"),
    ({"cost": {"type": "per_result", "value": 1}}, "a person picks its reserve"),
    ({"cost": {"type": "per_success", "value": 1, "tavily_rates": {}}}, "single-provider key"),
    ({"observed_cost": 0.1}, "no cost"),
    ({"cost": {"type": "per_success", "value": 1}, "miss": {"status": 404, "means": "x"}}, "one `miss` rule"),
])
def test_a_row_with_no_zero_diff_rewrite_stays_as_it_is(row, reason):
    with pytest.raises(codemod.Refused, match=reason):
        codemod.rewrite({"id": "demo.x", **row}, provider="demo", adapter={"miss": "a == null", "verified": True},
                        check_branches=False)


def test_a_provider_decided_in_python_waits_for_its_batch():
    with pytest.raises(codemod.Refused, match="settle.py"):
        codemod.rewrite({"id": "tikhub.x", "cost": {"type": "per_success", "value": 1}}, provider="tikhub")


FILE = """provider: demo
endpoints:
  # the curated row
  - id: demo.x
    path: /x
    cost:
      type: per_success  # why
      value: 1
    # kept: it belongs to the note
    note: hello
  - id: demo.y
    cost: {type: free}
"""


def test_applying_swaps_only_the_replaced_keys_and_names_the_dropped_comment():
    change = codemod.rewrite({"id": "demo.x", "cost": {"type": "per_success", "value": 1, **PROVENANCE}},
                             provider="demo", check_branches=False)
    text, dropped = codemod.apply_rows(FILE, {"demo.x": change})
    assert "# the curated row" in text and "# kept: it belongs to the note" in text and "note: hello" in text
    assert dropped == ["type: per_success  # why"]
    assert yaml.safe_load(text)["endpoints"][0]["price"] == {"value": "1"}
    assert yaml.safe_load(text)["endpoints"][1] == {"id": "demo.y", "cost": {"type": "free"}}


def test_applying_moves_the_adapter_miss_out_of_adapters_yaml():
    text = "adapters:\n  demo.x:\n    out: {a: b}\n    miss: \"a == []\"\n  demo.y:\n    miss: \"b == []\"\n"
    assert codemod.drop_adapter_misses(text, {"demo.x"}) == \
        "adapters:\n  demo.x:\n    out: {a: b}\n  demo.y:\n    miss: \"b == []\"\n"
