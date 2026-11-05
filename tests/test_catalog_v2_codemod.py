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
                                             {"path": "response.results", "op": "empty"}]}, True),
    ("status == 'NOT_FOUND'", {"path": "response.status", "op": "equals", "value": "NOT_FOUND"}, False),
    ("data[0].found == false", {"path": "response.data.0.found", "op": "equals", "value": False}, False),
    ("code == 404", {"path": "response.code", "op": "equals", "value": 404}, False),
    (". == []", [{"path": "response", "op": "present"}, {"path": "response", "op": "empty"}], True),
    ("contacts == []", [{"path": "response.contacts", "op": "present"},
                        {"path": "response.contacts", "op": "empty"}], True),
])
def test_an_adapter_expression_becomes_a_predicate(expr, predicate, widens):
    warnings: list[str] = []
    assert codemod.predicate(expr, warnings) == predicate
    assert bool(warnings) is widens


@pytest.mark.parametrize("names", [["results"], ["data.items", "data.videos"], ["a", "b.c", "d.e"]])
def test_a_coalesce_miss_reads_as_today_on_every_list_or_missing_value(names):
    import itertools

    from treg.domain.catalog.routing.paths import evaluate
    from treg.domain.catalog.schema.paths import holds

    expr = f"coalesce({', '.join(names)}, []) == []"
    predicate = codemod.predicate(expr, [])
    for values in itertools.product(("missing", None, "", [], [1], 0), repeat=len(names)):
        doc: dict = {}
        for name, value in zip(names, values, strict=True):
            if value != "missing":
                *parents, last = name.split(".")
                node = doc
                for parent in parents:
                    node = node.setdefault(parent, {})
                node[last] = value
        assert holds(predicate, {"response": doc}) == bool(evaluate(expr, doc)), (expr, doc)


@pytest.mark.parametrize("expr", ["coalesce(" + " ," * 50_000 + ") == []", "coalesce(a," + " " * 50_000 + "x) == []",
                                  "coalesce(" + ", [" * 30_000 + ") == []", "coalesce(" + "a," * 50_000 + "[]) == []"])
def test_a_long_adversarial_coalesce_is_judged_in_linear_time(expr):
    import time

    started = time.perf_counter()
    try:
        codemod.predicate(expr, [])
    except codemod.Refused:
        pass
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize("expr", ["coalesce(a, [], []) == []", "coalesce(, []) == []", "coalesce([]) == []",
                                  "coalesce(f(a), []) == []", "coalesce(a, b) == []"])
def test_a_coalesce_that_is_not_paths_then_an_empty_list_is_refused(expr):
    with pytest.raises(codemod.Refused):
        codemod.predicate(expr, [])


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


def _table_row(**keys) -> dict:
    return {"id": "demo.x", **keys, "cost": {
        "type": "per_success", "currency": "USD", "settle": "table",
        "table": [{"when": {"body.model": "m1", "queryParams.size": 2}, "value": 0.28}],
        "fallback": {"value": 0.5, "note": "ceiling"}, **PROVENANCE}}


def test_a_table_row_with_an_adapter_miss_freezes_its_outcome():
    change = codemod.rewrite(_table_row(), provider="demo", adapter={"miss": "items == null", "verified": True},
                             check_branches=False)
    assert change.keys["price"]["table"] == [{"when": [
        {"path": "body.model", "op": "equals", "value": "m1"},
        {"path": "query.size", "op": "equals", "value": 2}], "value": "0.28"}]
    assert change.keys["miss"] == {"when": {"path": "response.items", "op": "absent"}}
    assert change.keys["error"] == {"when": codemod.ENVELOPE} and change.keys["bill"] == {"error": "charged"}
    assert change.adapter_miss and change.replaced == ("cost",) and _v2(change)


@pytest.mark.parametrize("miss", ["items == null", "coalesce(items, []) == []"])
def test_a_per_success_row_with_an_expect_keeps_its_error_envelope_out_of_the_error_rule(miss):
    change = codemod.rewrite(_table_row(expect={"json_path": "base.code", "equals": 0}), provider="demo",
                             adapter={"miss": miss, "verified": True}, check_branches=False)
    assert "error" not in change.keys and "bill" not in change.keys
    assert change.keys["expect"] == {"path": "response.base.code", "op": "equals", "value": 0}
    assert change.replaced == ("cost", "expect") and change.adapter_miss and _v2(change)
    assert any("stats and archive admission stop calling a truthy `error`" in w for w in change.warnings)


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
        codemod.rewrite({"id": "apollo.x", "cost": {"type": "per_success", "value": 1}}, provider="apollo")


DEFAULTS = {"bill": {"miss": "charged", "error": "charged"}, "provenance": PROVENANCE}


def test_a_row_is_written_against_its_providers_defaults():
    adapter = {"miss": "data == null", "verified": True}
    priced = codemod.rewrite({"id": "demo.x", "cost": {"type": "per_success", "value": 0.001, **PROVENANCE}},
                             provider="demo", adapter=adapter, check_branches=False, defaults=DEFAULTS)
    assert priced.keys == {"price": {"value": "0.001"},
                           "error": {"when": codemod.ENVELOPE},
                           "miss": {"when": {"path": "response.data", "op": "absent"}}}
    free = codemod.rewrite({"id": "demo.y", "cost": {"type": "free"}}, provider="demo",
                           check_branches=False, defaults=DEFAULTS)
    assert free.keys == {"price": {"value": "0"}, "provenance": None}
    unpriced = codemod.rewrite({"id": "demo.z", "tier": "extended"}, provider="demo",
                               check_branches=False, defaults=DEFAULTS)
    assert unpriced.keys == {"provenance": None} and unpriced.replaced == ("tier",)
    ruled = codemod.rewrite({"id": "demo.w", "tier": "extended", "expect": {"path": "response.ok", "op": "present"}},
                            provider="demo", check_branches=False, defaults=DEFAULTS)
    assert ruled.keys == {"provenance": None} and ruled.replaced == ("tier",)


def test_an_unpriced_row_moves_its_outcome_keys_to_v2_with_its_opt_out():
    expected = codemod.rewrite({"id": "demo.z", "tier": "extended", "expect": {"json_path": "code", "equals": 0}},
                               provider="demo", check_branches=False, defaults=DEFAULTS)
    assert expected.keys == {"provenance": None, "expect": {"path": "response.code", "op": "equals", "value": 0}}
    assert expected.replaced == ("tier", "expect") and _v2(expected)
    missed = codemod.rewrite({"id": "demo.z", "miss": {"status": 404, "means": "No such user."}}, provider="demo",
                             check_branches=False, defaults=DEFAULTS)
    assert missed.keys == {"provenance": None, "miss": {"status": [404], "means": "No such user."}}
    assert missed.replaced == ("miss",) and _v2(missed)
    adapted = codemod.rewrite({"id": "demo.z"}, provider="demo", adapter={"miss": "data == null", "verified": True},
                              check_branches=False, defaults=DEFAULTS)
    assert adapted.keys == {"provenance": None, "error": {"when": codemod.ENVELOPE},
                            "miss": {"when": {"path": "response.data", "op": "absent"}}} and adapted.adapter_miss


@pytest.mark.parametrize("row,adapter,reason", [
    ({"miss": {"status": 404, "means": "x"}}, {"miss": "a == null", "verified": True}, "one `miss` rule"),
    ({"expect": {"status": 200}}, None, "does not parse: expect"),
])
def test_an_unpriced_row_whose_outcome_keys_have_no_v2_rewrite_stays_as_it_is(row, adapter, reason):
    with pytest.raises(codemod.Refused, match=reason):
        codemod.rewrite({"id": "demo.z", "tier": "extended", **row}, provider="demo", adapter=adapter,
                        check_branches=False, defaults=DEFAULTS)


def test_an_inert_2xx_miss_status_leaves_its_means_in_the_note():
    change = codemod.rewrite({"id": "demo.x", "note": "Pass a fullname.", "miss": {"status": 200, "means": "Billed."},
                              "cost": {"type": "per_success", "value": 1, **PROVENANCE}},
                             provider="demo", check_branches=False)
    assert change.keys["note"] == "Pass a fullname. Billed." and "miss" not in change.keys
    assert change.replaced == ("cost", "miss", "note")


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


ANCHORED = """provider: demo
endpoints:
  - id: demo.x
    cost: &one {type: per_success, value: 1, source: docs, source_url: 'https://example.com', checked: '2026-10-01', confidence: documented}
  - id: demo.y
    cost: *one
  - id: demo.z
    expect: &ok {json_path: ok, equals: true}
    cost: {type: free}
"""


def test_a_row_whose_replaced_key_defines_an_anchor_another_row_aliases_is_not_rewritten():
    rows = yaml.safe_load(ANCHORED)["endpoints"]
    changes = {row["id"]: codemod.rewrite(row, provider="demo", check_branches=False) for row in rows}
    assert codemod.anchored(ANCHORED, changes) == {"demo.x": "cost"}
    del changes["demo.x"]
    text, _ = codemod.apply_rows(ANCHORED, changes)
    assert [row.get("price") for row in yaml.safe_load(text)["endpoints"]] == [None, {"value": "1"}, {"value": "0"}]


def test_a_rewrite_that_does_not_parse_as_a_v2_row_is_refused():
    cost = {"type": "per_success", "value": None, "source": "inferred", "checked": "2026-08-07",
            "confidence": "unknown", "note": "unpriced"}
    with pytest.raises(codemod.Refused, match="the rewritten row does not parse: provenance"):
        codemod.rewrite({"id": "demo.x", "cost": cost}, provider="demo", check_branches=False)


def test_an_async_opt_out_becomes_null_beside_an_async_default_and_goes_without_one():
    row = {"id": "demo.x", "async": False, "cost": {"type": "free"}}
    beside = codemod.rewrite(row, provider="demo", check_branches=False, defaults={"async": {"id_from": "id"}})
    assert beside.keys["async"] is None and "async" in beside.replaced
    alone = codemod.rewrite(row, provider="demo", check_branches=False)
    assert "async" not in alone.keys and "async" in alone.replaced


def test_an_async_opt_out_of_a_header_block_without_an_async_default_is_refused():
    row = {"id": "demo.x", "async": False, "cost": {"type": "free"}}
    with pytest.raises(codemod.Refused, match="opts out of the header's async block"):
        codemod.rewrite(row, provider="demo", check_branches=False, header_async={"id_from": "id"})


def test_a_fallback_beside_its_equal_value_folds_its_note_into_the_price():
    cost = {"type": "per_call", "value": 0.0005, "fallback": {"value": 0.0005, "note": "a small ceiling"},
            "note": "Settles at usage.cost.", "source": "docs", "source_url": "https://example.com",
            "checked": "2026-09-23", "confidence": "verified"}
    change = codemod.rewrite({"id": "demo.x", "cost": cost}, provider="demo", check_branches=False)
    assert change.keys["price"] == {"value": "0.0005", "note": "Settles at usage.cost. a small ceiling"}
    with pytest.raises(codemod.Refused, match="cost.fallback beside a different cost.value"):
        codemod.rewrite({"id": "demo.x", "cost": {**cost, "value": 0.001}}, provider="demo", check_branches=False)


@pytest.mark.parametrize("provider", ["hunter", "tikhub"])
def test_the_codemod_runs_on_the_real_catalog(provider, capsys):
    assert codemod.main(["--provider", provider]) == 0
    assert f"cannot rewrite {provider}." in capsys.readouterr().out


def test_only_a_provider_with_a_row_in_todays_spelling_has_its_defaults_withheld(tmp_path):
    header = "defaults:\n  provenance: {source: docs, checked: '2026-10-01', confidence: documented}\n"
    (tmp_path / "demo.yaml").write_text(f"provider: demo\n{header}endpoints:\n  - id: demo.x\n    price: {{value: '1'}}\n")
    (tmp_path / "demox.yaml").write_text("provider: demox\nendpoints:\n  - id: demox.x\n    cost: {type: free}\n")
    (tmp_path / "mid.yaml").write_text(f"provider: mid\n{header}endpoints:\n  - id: mid.x\n    price: {{value: '1'}}\n")
    (tmp_path / "mid.extended.yaml").write_text("provider: mid\nendpoints:\n  - id: mid.y\n    cost: {type: free}\n")
    assert codemod.withheld(tmp_path) == {tmp_path / "mid.yaml"}
