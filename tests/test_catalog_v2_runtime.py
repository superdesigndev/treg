"""The call runtime reading v2 rows: the one path and predicate evaluation, the one outcome
classification, and the v2 settlement order through the seams every call uses."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field

import pytest

from treg.application.call import cost
from treg.application.call.resolve import MarketplaceCall
from treg.application.call.settle import decide_settle
from treg.domain.catalog import outcome
from treg.domain.catalog.schema import V2Row, served
from treg.domain.catalog.schema.paths import holds, select
from treg.domain.money import settlement

PROVENANCE = {"source": "docs", "source_url": "https://example.com", "checked": "2026-10-01",
              "confidence": "documented"}


@dataclass
class Rates:
    credit_rates: dict = field(default_factory=lambda: {"demo": 0.02})
    unit_rates: dict = field(default_factory=lambda: {"demo": {"page": 0.5}})
    fx: dict = field(default_factory=lambda: {"USD": 1.0, "CNY": 0.14})


def row(input_schema: dict | None = None, **keys) -> dict:
    keys.setdefault("price", {"value": "1", "currency": "credit"})
    if keys["price"] and (keys["price"].get("value") not in (None, "0") or keys["price"].keys() & {"table", "rates"}):
        keys.setdefault("provenance", PROVENANCE)
    parsed = V2Row.model_validate({"id": "demo.x", "method": "POST", "path": "/x", **keys})
    return {"id": "demo.x", "provider": "demo", "method": "POST", "input": input_schema or {},
            **served(parsed, input_schema or {})}


def call(ep: dict, body: dict | None = None, query: list | None = None, *, status=200, answer=b"{}",
         headers=None, routed=False):
    request = settlement.request_evidence(query or [], json.dumps(body).encode() if body else b"")
    hold, unit, credit, terms = cost.price(ep, Rates(), request)
    basis = cost.basis(Rates(), ep, hold, terminal=False)
    mk = MarketplaceCall(tool=None, upstream="", consumed=set(), endpoint_id=ep["id"], provider="demo",
                         tier="credential", estimate_micro=hold, unit_micro=unit,
                         reported_charge_unit_micro=credit, settlement_basis=basis, request_data=request,
                         cost_terms=terms, call_id="c", deferred=[] if routed else None,
                         cost_type=(ep.get("cost") or {}).get("type") or "")
    raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
    return hold, decide_settle(mk, status, raw, headers=headers)


# -- paths and predicates ------------------------------------------------------------------------

DOC = {"response": {"items": [{"k": "A", "n": 1}, None, {"k": "B", "n": 2}], "empty": [], "nil": None,
                    "flag": True, "count": 20000, "text": "Will NOT be charged"},
       "body": {"limit": 5}, "query": {"q": "x"}}


@pytest.mark.parametrize("path,values", [
    ("response.items.0.n", [1]), ("response.items.*.n", [1, 2]), ("response.items[k=B].n", [2]),
    ("response.nil", [None]), ("response.missing", []), ("response.items.9", []), ("body.limit", [5]),
    ("query.q", ["x"]), ("header.x", []),
])
def test_a_path_selects_every_value_it_names_and_nothing_when_missing(path, values):
    assert select(path, DOC) == values


@pytest.mark.parametrize("predicate,expected", [
    ({"path": "response.nil", "op": "absent"}, True),
    ({"path": "response.missing", "op": "absent"}, True),
    ({"path": "response.missing", "op": "empty"}, False),
    ({"path": "response.empty", "op": "empty"}, True),
    ({"path": "response.nil", "op": "present"}, False),
    ({"path": "response.count", "op": "equals", "value": "20000"}, False),
    ({"path": "response.flag", "op": "equals", "value": 1}, False),
    ({"path": "response.count", "op": "in", "value": [1, 20000]}, True),
    ({"path": "response.text", "op": "contains", "value": "not be charged"}, True),
    ([{"path": "body.limit", "op": "equals", "value": 5}, {"path": "response.flag", "op": "equals", "value": True}], True),
    ({"any": [{"path": "response.missing", "op": "present"}, {"path": "query.q", "op": "equals", "value": "x"}]}, True),
])
def test_a_predicate_holds_by_its_op_with_exact_equality(predicate, expected):
    assert holds(predicate, DOC) is expected


# -- the outcome classification ------------------------------------------------------------------

def test_the_outcome_is_judged_in_order_status_expect_error_204_miss():
    ep = row(expect={"path": "response.code", "op": "equals", "value": 0},
             error={"when": {"path": "response.error", "op": "present"}},
             miss={"status": [404], "when": {"path": "response.reason", "op": "equals", "value": "none"}})
    judge = lambda status, doc: outcome.judge(ep, status, doc)  # noqa: E731
    assert judge(404, {"reason": "none"}) == outcome.Outcome("miss", "status")
    assert judge(404, {"reason": "fault"}) == outcome.Outcome("error", "status")
    assert judge(200, {"code": 1}) == outcome.Outcome("error", "expect")
    assert judge(200, {"code": 0, "error": "x"}) == outcome.Outcome("error", "error")
    assert judge(204, {"code": 0}) == outcome.Outcome("miss", "no_content")
    assert judge(200, {"code": 0}) == outcome.Outcome("hit")
    assert outcome.expect_failure(ep, {"code": 7}) == "code=7"
    two = row(miss={"when": {"path": "response.items", "op": "empty"}})
    assert outcome.judge(two, 200, {"items": []}).state == "miss" and outcome.declares_miss(two)


def test_an_outcome_plugin_that_raises_is_false(monkeypatch):
    module = types.ModuleType("treg.domain.plugins.demo")
    module.boom = lambda request, answer: 1 / 0
    module.found_nothing = lambda request, answer: answer is None or answer.body == {}
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    assert outcome.judge(row(miss={"plugin": "demo.boom"}), 200, {}).state == "hit"
    assert outcome.judge(row(miss={"plugin": "demo.found_nothing"}), 200, {}).state == "miss"


def test_routing_hands_an_outcome_plugin_the_answer_it_holds(monkeypatch):
    from treg.domain.catalog.routing.contracts import Adapter, declared_miss
    module = types.ModuleType("treg.domain.plugins.demo")
    module.gone = lambda request, answer: answer.status == 404 and answer.raw == b'{"e": 1}'
    module.empty = lambda request, answer: answer.status == 200 and answer.body == {"items": []}
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    assert declared_miss(row(miss={"status": [404], "plugin": "demo.gone"}), 404, b'{"e": 1}')
    adapter = Adapter(endpoint_id="demo.x", accepts=(), in_map={}, out_map={}, miss="")
    assert adapter.is_miss({"items": []}, row(miss={"plugin": "demo.empty"}))
    assert not adapter.is_miss({"items": [1]}, row(miss={"plugin": "demo.empty"}))


def test_routing_reads_a_v2_error_envelope_and_a_204_as_misses():
    from types import SimpleNamespace

    from treg.application.call.route import child_outcome
    from treg.domain.catalog.routing.contracts import Adapter
    ep = row(error={"when": {"path": "response.error", "op": "present"}})
    adapter = Adapter(endpoint_id="demo.x", accepts=(), in_map={}, out_map={"email": "contacts[0].email"},
                      miss="")
    contract = SimpleNamespace(required_output=("email",))
    read = lambda status, body: child_outcome(ep, contract, adapter, status, body, tier="platform")  # noqa: E731
    found = b'{"contacts": [{"email": "a@b.co"}]}'
    assert read(200, found).kind == "hit"
    envelope = read(200, b'{"contacts": [{"email": "a@b.co"}], "error": "partial"}')
    assert (envelope.kind, envelope.why) == ("miss", ("provider_error",))
    assert (read(204, found).kind, read(204, found).why) == ("miss", ("adapter_miss",))
    assert read(204, b"").why == ("non_json", "adapter_miss", "empty_core")


# -- the hold ------------------------------------------------------------------------------------

def test_a_value_price_holds_whole_per_blocks_when_it_rounds_up():
    rows = {"rows": {"param": "body.limit", "default": {"value": 100, "source": "docs"}, "max": 10000}}
    linear = row(price={"value": "1", "currency": "credit", "per": 10}, reserve=rows)
    blocks = row(price={"value": "1", "currency": "credit", "per": 10, "round": "up"}, reserve=rows)
    assert call(linear, {"limit": 15})[0] == 30000 and call(blocks, {"limit": 15})[0] == 40000
    assert call(blocks, {})[0] == 200000                                # the default: 100 rows
    assert call(blocks, {"limit": 99999})[0] == 20000000                # clamped at max
    assert call(blocks, {"limit": "x"})[0] == 200000                    # not a count: the default


def test_entities_add_up_list_items_and_comma_separated_query_values():
    ep = row(price={"value": "0.001"}, reserve={"entities": {"from": ["body.domains", "query.target"], "max": 4}})
    assert call(ep, {"domains": ["a", "b"]}, [("target", "c,d")])[0] == 4000
    assert call(ep, {"domains": ["a", "b", "c"]}, [("target", "d,e")])[0] == 4000
    assert call(ep, {})[0] == 1000                                      # never below one


def test_a_price_table_matches_after_input_defaults_and_coercion_and_falls_back():
    input_schema = {"body": {"size": {"type": "string", "default": "1K"},
                             "seconds": {"type": "integer", "min": 1, "max": 10}}}
    ep = row(input_schema, price={"currency": "USD", "fallback": {"value": "0.9", "note": "ceiling"}, "table": [
        {"when": [{"path": "body.size", "op": "equals", "value": "1K"}], "value": "0.1"},
        {"when": [{"path": "body.size", "op": "equals", "value": "2K"}], "value": "0.05", "times": "body.seconds"}]})
    assert call(ep, {})[0] == 100000
    assert call(ep, {"size": "2K", "seconds": 4})[0] == 200000
    assert call(ep, {"size": "2K", "seconds": 40})[0] == 900000         # out of range: the fallback
    assert call(ep, {"size": "8K"})[0] == 900000
    assert ep["cost"]["table"][0] == {"when": {"body.size": "1K"}, "value": 0.1}


def test_a_reserve_plugin_is_priced_at_the_frozen_rates_and_capped(monkeypatch):
    module = types.ModuleType("treg.domain.plugins.demo")
    module.hold = lambda request: {"result": request.body["n"], "start": 1}
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    ep = row(price={"rates": {"result": "0.01", "start": "0.1"}, "currency": "USD"},
             reserve={"plugin": "demo.hold", "max": "0.5"}, settle={"plugin": "demo.hold"})
    assert call(ep, {"n": 3})[0] == 130000 and call(ep, {"n": 100})[0] == 500000


def test_a_rates_plugin_that_returns_a_plain_number_is_missing_evidence(monkeypatch):
    module = types.ModuleType("treg.domain.plugins.demo")
    module.hold = lambda request: {"result": 2}
    module.used = lambda request, answer: 1
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    ep = row(price={"rates": {"result": "0.01"}, "currency": "USD"},
             reserve={"plugin": "demo.hold", "max": "0.5"}, settle={"plugin": "demo.used"})
    hold, decision = call(ep)
    assert hold == 20000 and decision.actual_micro == 20000


# -- the settlement order ------------------------------------------------------------------------

REPORTED = {"reported": {"path": "response.used", "unit": "credit"}}


@pytest.mark.parametrize("status,answer,billable,actual", [
    (200, {"used": 3}, True, 60000),
    (200, {"used": 0}, True, 0),
    (200, {"used": -1}, True, 20000),                                   # unreadable: the hold
    (200, {}, True, 20000),
    (400, {"used": 2}, True, 40000),                                    # a 4xx that reports a charge
    (400, {"used": 0}, False, None),
    (429, {"used": 2}, False, None),                                    # not the caller's fault
    (500, {"used": 2}, False, None),
])
def test_a_reported_charge_settles_uncapped_and_a_4xx_only_when_it_reports_one(status, answer, billable, actual):
    _, decision = call(row(settle=REPORTED), status=status, answer=answer)
    assert (decision.billable, decision.actual_micro) == (billable, actual)


@pytest.mark.parametrize("bill,status,reason", [
    ({"miss": "charged"}, 400, "rejected_unbilled_400"),               # today's per_call label
    ({"miss": "charged"}, 429, ""),                                     # not the caller's fault
    (None, 400, ""),
])
def test_an_unbilled_4xx_is_labelled_as_todays_per_call_row_labels_it(bill, status, reason):
    keys = {"bill": bill} if bill else {}
    _, decision = call(row(**keys), status=status, answer={})
    assert (decision.billable, decision.reason) == (False, reason)


def test_free_when_reads_the_request_and_the_answer_before_a_reported_charge():
    ep = row(settle=REPORTED, bill={"free_when": {"any": [{"path": "query.simplified", "op": "equals", "value": True}]}},
             input_schema={"queryParams": {"simplified": {"type": "boolean"}}})
    assert call(ep, query=[("simplified", "true")], answer={"used": 5})[1].actual_micro == 0
    assert call(ep, query=[("simplified", "false")], answer={"used": 5})[1].actual_micro == 100000


def test_the_outcome_and_bill_decide_an_unreported_answer():
    miss = {"when": {"path": "response.items", "op": "empty"}}
    error = {"when": {"path": "response.error", "op": "present"}}
    plain = row(miss=miss, error=error)
    charged = row(miss=miss, error=error, bill={"miss": "charged", "error": "charged"})
    units = row(price={"value": "1", "currency": "credit"}, reserve={"rows": {
        "param": "body.n", "default": {"value": 5, "source": "docs"}, "max": 5}}, miss=miss, bill={"miss": 2})
    assert [call(plain, answer=a)[1].actual_micro for a in ({"items": []}, {"error": 1}, {"items": [1]})] == [0, 0, 20000]
    assert [call(charged, answer=a)[1].actual_micro for a in ({"items": []}, {"error": 1})] == [20000, 20000]
    assert call(units, answer={"items": []})[1].actual_micro == 40000
    assert call(plain, status=204, answer=b"")[1].actual_micro == 0


def test_a_free_outcome_without_settle_evidence_observes_a_zero_charge():
    ep = row(miss={"when": {"path": "response.items", "op": "empty"}},
             expect={"path": "response.code", "op": "equals", "value": 0})
    for answer in ({"code": 0, "items": []}, {"code": 1, "items": [1]}):
        assert (call(ep, answer=answer)[1].observed_micro, call(ep, answer=answer)[1].actual_micro) == (0, 0)
    assert call(ep, answer={"code": 0, "items": [1]})[1].observed_micro is None


def test_a_count_settle_counts_non_null_items_capped_at_the_hold():
    ep = row(reserve={"rows": {"param": "body.n", "default": {"value": 3, "source": "docs"}, "max": 10}},
             settle={"count": {"path": "response.items"}})
    many = row(settle={"count": {"path": "response.items.*.id"}},
               reserve={"rows": {"param": "body.n", "default": {"value": 9, "source": "docs"}, "max": 9}})
    assert call(ep, answer={"items": [1, None, 2]})[1].actual_micro == 40000
    assert call(ep, answer={"items": [1, 2, 3, 4, 5]})[1].observed_micro == 60000
    assert call(ep, answer={"items": "x"})[1].actual_micro == 60000      # unreadable: the hold
    assert call(many, answer={"items": [{"id": 1}, {"id": None}, {}]})[1].actual_micro == 20000


def test_usage_meters_settle_from_the_frozen_basis_and_may_exceed_the_hold():
    ep = row(price={"value": "0.01"}, settle={"usage": {"unit": "usd", "terms": [
        {"path": "response.usage.in", "rate": "0.001"}, {"path": "response.usage.details[kind=IMG].n", "rate": "0.01"}]}})
    assert call(ep, answer={"usage": {"in": 10, "details": [{"kind": "TXT", "n": 5}, {"kind": "IMG", "n": 3}]}})[1] \
        .actual_micro == 40000
    assert call(ep, answer={"other": 1})[1].actual_micro == 10000


def test_a_header_report_is_scaled_and_a_settle_plugin_without_evidence_is_the_hold(monkeypatch):
    module = types.ModuleType("treg.domain.plugins.demo")
    module.rows = lambda request, answer: len(answer.body["rows"]) if "rows" in answer.body else None
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    header = row(settle={"reported": {"header": "x-credit", "unit": "credit", "scale": -1}})
    assert call(header, headers={"x-credit": "-2"})[1].actual_micro == 40000
    plugin = row(reserve={"rows": {"param": "body.n", "default": {"value": 4, "source": "docs"}, "max": 4}},
                 settle={"plugin": "demo.rows"})
    assert call(plugin, answer={"rows": [1, 2]})[1].actual_micro == 40000
    assert call(plugin, answer={})[1].actual_micro == 80000


def test_a_routed_child_answer_that_costs_nothing_is_released_for_its_parent():
    _, decision = call(row(miss={"when": {"path": "response.items", "op": "empty"}}), answer={"items": []},
                       routed=True)
    assert (decision.billable, decision.reason) == (False, "free_answer")


def test_an_async_terminal_answer_settles_by_the_order_with_the_submission_report():
    ep = row(settle=REPORTED, miss={"when": {"path": "response.items", "op": "empty"}})
    hold, _, _, terms = cost.price(ep, Rates(), settlement.request_evidence([], b""))
    basis = cost.persisted({**cost.basis(Rates(), ep, hold, terminal=True), "submission_charge_micro": 7}, terms)
    assert cost.settle_terminal(json.loads(json.dumps(basis)), "success", {"items": [1]}) == 7
    assert cost.settle_terminal(basis, "success", {"items": [1], "used": 1}) == 20000
    assert cost.settle_terminal(basis, "billed_failure", {}) == 7
    assert cost.settle_terminal({**basis, "submission_charge_micro": None}, "billed_failure", {}) == hold
    legacy = {"when": "terminal", "amount": {"kind": "observed"}, "fallback_micro": 9, "reserve_micro": 9}
    assert cost.settle_terminal(legacy, "success", {}) == 9


def test_a_spooled_v2_row_keeps_every_answer_path_its_rules_read():
    ep = row(price={"value": "0.01"}, expect={"path": "response.meta.ok", "op": "equals", "value": True},
             settle={"usage": {"unit": "usd", "terms": [{"path": "response.usage.details[kind=IMG].n", "rate": "1"}]}},
             miss={"when": {"path": "response.items.*.id", "op": "absent"}})
    assert cost.evidence_paths(ep) == ("usage", "meta.ok", "items")


# -- refusals, served forms and evidence the review of the first v2 rows found -------------------

@pytest.mark.parametrize("hold", [lambda request: 1 / 0, lambda request: "many"])
def test_a_reserve_plugin_without_a_quantity_refuses_the_call_before_any_hold(monkeypatch, hold):
    from treg.application.call.types import ResolutionFailed
    module = types.ModuleType("treg.domain.plugins.demo")
    module.hold = hold
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    ep = row(price={"value": "0.01"}, reserve={"plugin": "demo.hold", "max": "0.5"})
    with pytest.raises(ResolutionFailed) as refused:
        call(ep)
    assert (refused.value.kind, refused.value.blame, refused.value.status_code) == \
        ("catalog_price_invalid", "treg", 503)


def test_free_when_compares_with_null_as_written():
    ep = row(bill={"free_when": {"path": "response.cost", "op": "equals", "value": None}})
    assert ep["bill"]["free_when"] == {"path": "response.cost", "op": "equals", "value": None}
    assert call(ep, answer={"cost": None})[1].actual_micro == 0
    assert call(ep, answer={"cost": 1})[1].actual_micro == 20000


@pytest.mark.parametrize("path,answer,actual", [
    ("response.items.*.id", {}, 200000),
    ("response.items.*.id", b"<html>", 200000),
    ("response.items.*.id", {"error": "x"}, 200000),
    ("response.items.*.id", {"items": "x"}, 200000),
    ("response.items.*.id", {"items": []}, 0),
    ("response.items.*.id", {"items": [{"id": 1}, {}]}, 20000),
    ("response.items[kind=a]", {}, 200000),
    ("response.items[kind=a]", {"items": [{"kind": "b"}]}, 0),
    ("response[kind=a]", {"kind": "a"}, 200000),
    ("response[kind=a]", [{"kind": "a"}, {"kind": "a"}], 40000),
    ("response.data.*.items.*", {"data": [{"x": 1}]}, 200000),
    ("response.data.*.items.*", {"data": [{"items": []}]}, 0),
    ("response.data.*.items.*", {"data": [{"items": [1, 2]}]}, 40000),
])
def test_a_many_valued_count_settles_at_the_hold_when_its_list_is_unreadable(path, answer, actual):
    ep = row(settle={"count": {"path": path}},
             reserve={"rows": {"param": "body.n", "default": {"value": 10, "source": "docs"}, "max": 10}})
    assert call(ep, answer=answer)[1].actual_micro == actual


def test_a_rates_row_is_served_at_its_reserve_max_so_treg_s_key_may_serve_it():
    from treg.domain.catalog import store
    ep = row(price={"rates": {"result": "0.01"}}, reserve={"plugin": "demo.hold", "max": "0.5"})
    assert store.load().cost_view(ep["cost"], "demo")["usd"] == 0.5
    assert store.load().platform_eligible(ep)


def test_a_v2_miss_opts_into_result_aware_caching_only_beside_a_verified_adapter(monkeypatch):
    from types import SimpleNamespace

    from treg.domain.catalog import results, store
    ep = row(miss={"when": {"path": "response.items", "op": "empty"}})
    for adapter, opted in ((None, False), (SimpleNamespace(verified=False, miss=""), False),
                           (SimpleNamespace(verified=True, miss=""), True)):
        loaded = SimpleNamespace(by_id={"demo.x": ep}, adapters={"demo.x": adapter} if adapter else {})
        monkeypatch.setattr(store, "load", lambda loaded=loaded: loaded)
        assert results.has_result_rules("demo.x") is opted
