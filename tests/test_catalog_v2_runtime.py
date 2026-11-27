"""The call runtime reading v2 rows: the one path and predicate evaluation, the one outcome
classification, and the v2 settlement order through the seams every call uses."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field

import httpx
import pytest

from treg.application.call import cost
from treg.application.call.resolve import MarketplaceCall
from treg.application.call.settle import decide_settle
from treg.application.call.types import ResolutionFailed
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
         headers=None, routed=False, cached_hit=False, cached_headers=None):
    request = settlement.request_evidence(query or [], json.dumps(body).encode() if body else b"")
    hold, unit, credit, terms = cost.price(ep, Rates(), request)
    basis = cost.basis(Rates(), ep, hold, terminal=False)
    mk = MarketplaceCall(tool=None, upstream="", consumed=set(), endpoint_id=ep["id"], provider="demo",
                         tier="credential", estimate_micro=hold, unit_micro=unit,
                         reported_charge_unit_micro=credit, settlement_basis=basis, request_data=request,
                         cost_terms=terms, call_id="c", deferred=[] if routed else None,
                         cost_type=(ep.get("cost") or {}).get("type") or "")
    raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
    return hold, decide_settle(mk, status, raw, headers=headers, cached_hit=cached_hit,
                               cached_headers=cached_headers)


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


def test_ok_when_beside_a_status_makes_both_a_4xx_and_an_empty_2xx_a_miss():
    ep = row(miss={"status": [404], "ok_when": {"path": "response", "op": "empty"}})
    assert outcome.judge(ep, 404, {"detail": "Not found."}) == outcome.Outcome("miss", "status")
    assert outcome.judge(ep, 200, []) == outcome.Outcome("miss", "miss")
    assert outcome.judge(ep, 200, [{"phone_number": "+15555550100"}]) == outcome.Outcome("hit")
    assert outcome.judge(ep, 400, {}) == outcome.Outcome("error", "status")
    with pytest.raises(ValueError, match="ok_when judges a 2xx beside status"):
        row(miss={"when": {"path": "response.a", "op": "absent"}, "ok_when": {"path": "response", "op": "empty"}})


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
    assert call(blocks, {"limit": "0" * 5000 + "15"})[0] == 40000       # leading zeros: still 15 rows


@pytest.mark.parametrize("query,body,refused", [
    ([("q", "a"), ("q", "b")], None, "q"),                              # a single value, sent twice
    ([("tags", "a"), ("tags", "b")], None, None),                       # a list may repeat
    ([("other", "a"), ("other", "b")], None, None),                     # undeclared: no type to judge
    ([("limit", "10"), ("limit", "10")], None, "limit"),
    ([], None, None),                                                   # absent: the default
    ([("limit", "100")], None, None), ([("limit", "0050")], None, None), ([("limit", "1")], None, None),
    ([("limit", "0")], None, "query.limit"), ([("limit", "101")], None, "query.limit"),
    ([("limit", "abc")], None, "query.limit"), ([("limit", "")], None, "query.limit"),
    ([("limit", " 5")], None, "query.limit"), ([("limit", "5.0")], None, "query.limit"),
    ([("limit", "-5")], None, "query.limit"), ([("limit", "\u0661\u0662")], None, "query.limit"),
    ([("limit", "9" * 30)], None, "query.limit"),
    ([("limit", "0" * 5000 + "5")], None, None), ([("limit", "0" * 5000 + "101")], None, "query.limit"),
    ([("limit", "0" * 5000)], None, "query.limit"),
])
def test_a_request_repeating_a_single_value_or_with_an_unreadable_count_is_refused(query, body, refused):
    from treg.application.call.types import ResolutionFailed
    input_schema = {"queryParams": {"q": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
                                    "limit": {"type": "integer"}}}
    ep = row(input_schema, price={"value": "1", "currency": "credit", "per": 10, "round": "up"},
             reserve={"rows": {"param": "query.limit", "default": {"value": 10, "source": "docs"}, "max": 100}})
    request = settlement.request_evidence(query, json.dumps(body).encode() if body else b"")
    if refused is None:
        cost.require_request(ep, request)
        return
    with pytest.raises(ResolutionFailed) as caught:
        cost.require_request(ep, request)
    assert caught.value.status_code == 400 and caught.value.detail["parameter"] == refused
    assert all(value not in json.dumps(caught.value.detail) for _, value in query if len(value) > 2)


def test_a_count_in_a_json_body_is_a_whole_number_or_a_string_of_digits():
    from treg.application.call.types import ResolutionFailed
    ep = row(reserve={"rows": {"param": "body.n", "default": {"value": 3, "source": "docs"}, "max": 10}})
    for n in (1, 10, "7", None):
        cost.require_request(ep, settlement.request_evidence([], json.dumps({"n": n}).encode()))
    for n in (0, 11, 2.5, 5.0, True, "x", [3]):
        with pytest.raises(ResolutionFailed):
            cost.require_request(ep, settlement.request_evidence([], json.dumps({"n": n}).encode()))


def test_entities_add_up_list_items_and_comma_separated_query_values():
    ep = row(price={"value": "0.001"}, reserve={"entities": {"from": ["body.domains", "query.target"], "max": 4}})
    assert call(ep, {"domains": ["a", "b"]}, [("target", "c,d")])[0] == 4000
    assert call(ep, {"domains": ["a", "b", "c"]}, [("target", "d,e")])[0] == 4000
    assert call(ep, {})[0] == 1000                                      # never below one


def test_treg_key_refuses_more_entities_than_the_max_before_any_hold():
    ep = row(price={"value": "0.001"}, reserve={"entities": {"from": ["body.domains", "query.target"], "max": 4}})
    fits = settlement.request_evidence([("target", "c,d")], json.dumps({"domains": ["a", "b"]}).encode())
    cost.require_entities(ep, fits)
    over = settlement.request_evidence([("target", "d,e")], json.dumps({"domains": ["a", "b", "c"]}).encode())
    with pytest.raises(ResolutionFailed) as exc:
        cost.require_entities(ep, over)
    assert exc.value.status_code == 400 and exc.value.detail["error"] == "catalog_parameter_invalid"


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
    (200, {"used": float("inf")}, True, 20000),
    (200, {"used": 1e30}, True, 20000),                                 # too large to price: the hold
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


def test_a_count_settle_counts_non_null_items_capped_at_the_declared_max():
    ep = row(reserve={"rows": {"param": "body.n", "default": {"value": 3, "source": "docs"}, "max": 10}},
             settle={"count": {"path": "response.items"}})
    many = row(settle={"count": {"path": "response.items.*.id"}},
               reserve={"rows": {"param": "body.n", "default": {"value": 9, "source": "docs"}, "max": 9}})
    assert call(ep, answer={"items": [1, None, 2]})[1].actual_micro == 40000
    assert call(ep, answer={"items": [1, 2, 3, 4, 5]})[1].observed_micro == 100000   # above the hold
    assert call(ep, answer={"items": list(range(12))})[1].actual_micro == 200000      # the max, 10 rows
    assert call(ep, answer={"items": "x"})[1].actual_micro == 60000      # unreadable: the hold
    assert call(many, answer={"items": [{"id": 1}, {"id": None}, {}]})[1].actual_micro == 20000


def test_a_count_settle_without_a_declared_max_is_capped_at_the_hold(monkeypatch):
    module = types.ModuleType("treg.domain.plugins.demo")
    module.rows = lambda request, answer: answer.body["n"]
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    single = row(settle={"plugin": "demo.rows"})
    undeclared = row(price={"value": "0.001"}, reserve={"entities": {"from": "body.domains"}},
                     settle={"plugin": "demo.rows"})
    declared = row(price={"value": "0.001"}, reserve={"entities": {"from": "body.domains", "max": 4}},
                   settle={"plugin": "demo.rows"})
    assert call(single, answer={"n": 5})[1].actual_micro == 20000                  # no reserve: the hold
    assert call(undeclared, {"domains": ["a"]}, answer={"n": 3})[1].actual_micro == 1000
    assert call(undeclared, {"domains": ["a"]}, answer={"n": 0})[1].actual_micro == 0
    assert call(declared, {"domains": ["a"]}, answer={"n": 3})[1].actual_micro == 3000  # above the hold
    assert call(declared, {"domains": ["a"]}, answer={"n": 9})[1].actual_micro == 4000  # the declared max


def test_usage_meters_settle_from_the_frozen_basis_and_may_exceed_the_hold():
    ep = row(price={"value": "0.01"}, settle={"usage": {"unit": "usd", "terms": [
        {"path": "response.usage.in", "rate": "0.001"}, {"path": "response.usage.details[kind=IMG].n", "rate": "0.01"}]}})
    assert call(ep, answer={"usage": {"in": 10, "details": [{"kind": "TXT", "n": 5}, {"kind": "IMG", "n": 3}]}})[1] \
        .actual_micro == 40000
    assert call(ep, answer={"other": 1})[1].actual_micro == 10000


def test_a_declared_worst_case_is_held_while_a_missing_meter_settles_at_the_estimate():
    ep = row(price={"value": "1", "currency": "credit"},
             settle={"usage": {"path": "response.cost", "unit": "credit"}, "worst_case": "15"})
    assert ep["cost"]["worst_case"] == 15
    assert cost.worst_case_micro(ep, Rates()) == 300000
    assert cost.worst_case_micro(row(settle={"usage": {"path": "response.cost", "unit": "credit"}}), Rates()) == 0
    hold, decision = call(ep, answer={"other": 1})
    assert hold == 20000 and decision.actual_micro == 20000
    with pytest.raises(ValueError, match="worst_case belongs beside"):
        row(settle={"count": {"path": "response.items"}, "worst_case": "15"})


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


def test_an_async_terminal_answer_settles_at_the_charge_reported_on_its_submission():
    ep = row(settle=REPORTED, miss={"when": {"path": "response.items", "op": "empty"}})
    hold, _, _, terms = cost.price(ep, Rates(), settlement.request_evidence([], b""))
    basis = cost.persisted({**cost.basis(Rates(), ep, hold, terminal=True), "submission_charge_micro": 7}, terms)
    assert cost.settle_terminal(json.loads(json.dumps(basis)), "success", {"items": [1]}) == 7
    assert cost.settle_terminal(basis, "success", {"items": [1], "used": 0}) == 7
    assert cost.settle_terminal(basis, "billed_failure", {"used": 0}) == 7
    unreported = {**basis, "submission_charge_micro": None}
    assert cost.settle_terminal(unreported, "success", {"items": [1], "used": 1}) == hold
    assert cost.settle_terminal(unreported, "success", {"items": [], "used": 1}) == 0
    assert cost.settle_terminal(unreported, "billed_failure", {"used": 1}) == hold
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


@pytest.mark.parametrize("miss", [{"when": {"path": "response.items", "op": "empty"}}, {"status": [404]}])
def test_a_v2_miss_opts_into_result_aware_caching_only_beside_a_verified_adapter(monkeypatch, miss):
    from types import SimpleNamespace

    from treg.domain.catalog import results, store
    ep = row(miss=miss)
    for adapter, opted in ((None, False), (SimpleNamespace(verified=False, miss=""), False),
                           (SimpleNamespace(verified=True, miss=""), True)):
        loaded = SimpleNamespace(by_id={"demo.x": ep}, adapters={"demo.x": adapter} if adapter else {})
        monkeypatch.setattr(store, "load", lambda loaded=loaded: loaded)
        assert results.has_result_rules("demo.x") is opted
    loaded = SimpleNamespace(by_id={"demo.x": row()}, adapters={"demo.x": SimpleNamespace(verified=True, miss="")})
    monkeypatch.setattr(store, "load", lambda: loaded)
    assert results.has_result_rules("demo.x") is False


@pytest.mark.parametrize("endpoint,verified", [
    ({**row(miss={"status": [404]}), "capability": "lookup"}, True),
    ({**row(miss={"when": {"path": "response.name", "op": "absent"}}), "capability": "lookup"}, True),
    ({**row(), "capability": "lookup"}, False),
    ({"capability": "lookup", "miss": {"status": 404, "means": "today's spelling"}}, False),
])
def test_an_adapter_without_a_miss_verifies_beside_any_v2_miss_rule(endpoint, verified):
    from pathlib import Path

    from treg.domain.catalog.routing.contracts import load_routing
    lookup = {"identity": [{"name": "str"}], "output": {"name": {"type": "str", "required": True}}, "filters": {}}
    adapter = {"accepts": [["name"]], "in": {"name": "queryParams.name"}, "out": {"name": "name"}}
    docs = {"contracts.yaml": {"contracts": {"lookup": lookup}}, "adapters.yaml": {"adapters": {"demo.x": adapter}}}
    endpoint = {**endpoint, "test_request": {"queryParams": {"name": "Example"}}}
    _, adapters = load_routing(Path("."), {"demo.x": endpoint}, lambda p: docs[p.name],
                               lambda ep: {"name": "Example"})
    assert adapters["demo.x"].verified is verified


@pytest.mark.parametrize("keys,rate", [
    ({"price": {"value": "1", "currency": "credit"}}, None),
    ({"price": {"value": "1", "currency": "credit"}}, "demo credit"),
    ({"price": {"value": "0.01"}, "settle": {"reported": {"path": "response.cost", "unit": "credit"}}},
     "demo credit"),
    ({"price": {"value": "0.01"}, "settle": {"usage": {"unit": "token", "path": "response.n"}}},
     "demo token"),
])
def test_treg_s_key_refuses_a_row_whose_credit_or_meter_rate_is_unknown(keys, rate):
    from treg.application.call.types import ResolutionFailed
    ep = row(**keys)
    rates = Rates(credit_rates={"demo": None if rate else 0.02})
    if rate is None:
        cost.require_rates(ep, rates)
        return
    with pytest.raises(ResolutionFailed) as refused:
        cost.require_rates(ep, rates)
    assert (refused.value.kind, refused.value.blame, refused.value.status_code) == \
        ("catalog_price_invalid", "treg", 503)
    assert refused.value.detail["endpoint_id"] == "demo.x" and refused.value.detail["rate"] == rate


# -- treg's-key bounds and an absent reported figure ---------------------------------------------

def test_platform_max_bounds_treg_key_only_and_must_sit_below_max():
    rows = {"param": "body.n", "default": {"value": 3, "source": "docs"}, "max": 1000, "platform_max": 100}
    ep = row({"body": {"n": {"type": "integer"}}}, reserve={"rows": rows})
    for n in (None, 1, 100):
        cost.require_rows(ep, settlement.request_evidence([], json.dumps({"n": n}).encode()))
    over = settlement.request_evidence([], json.dumps({"n": 101}).encode())
    cost.require_request(ep, over)                                      # every tier: up to max
    with pytest.raises(ResolutionFailed) as exc:
        cost.require_rows(ep, over)                                     # treg's key: up to platform_max
    assert exc.value.status_code == 400 and exc.value.detail["parameter"] == "body.n"
    assert ep["reserve"]["rows"]["platform_max"] == 100
    for bad in ({**rows, "platform_max": 1000}, {**rows, "platform_max": 2},
                {"default": {"value": 3, "source": "legacy"}, "max": 10, "platform_max": 5}):
        with pytest.raises(ValueError):
            row(reserve={"rows": bad})


QUERY_ROWS = {"param": "query.limit", "default": {"value": 30, "source": "docs"}, "max": 1000, "platform_max": 100}


@pytest.mark.parametrize("raw", [
    b'{"limit": 1000}', b'{"limit": 5, "location": "US"}', b'{"limit": null}',
    b"limit=1000", b"location=US&limit=5", b"limit[]=5",
])
def test_treg_key_refuses_a_query_rows_parameter_sent_in_the_body(raw):
    ep = row({"queryParams": {"limit": {"type": "integer"}, "location": {"type": "string"}}},
             reserve={"rows": QUERY_ROWS})
    request = settlement.request_evidence([("limit", "5")], raw)
    with pytest.raises(ResolutionFailed) as exc:
        cost.require_rows(ep, request, raw)
    assert exc.value.status_code == 400 and exc.value.detail["parameter"] == "body.limit"
    assert "query parameter limit" in exc.value.detail["message"]


@pytest.mark.parametrize("raw", [
    b"", b"{}", b'{"location": "US", "active_only": true}', b'{"filters": {"limit": 1000}}',
    b'[{"limit": 1000}]', b"location=US", b"not a form", "limit=5".encode("utf-16"),
])
def test_a_body_without_the_query_rows_name_passes(raw):
    ep = row({"queryParams": {"limit": {"type": "integer"}}}, reserve={"rows": QUERY_ROWS})
    cost.require_rows(ep, settlement.request_evidence([("limit", "5")], raw), raw)
    cost.require_rows(ep, settlement.request_evidence([], raw), raw)


def test_a_body_rows_parameter_may_be_sent_in_the_body():
    rows = {"param": "body.limit", "default": {"value": 3, "source": "docs"}, "max": 1000, "platform_max": 100}
    ep = row({"body": {"limit": {"type": "integer"}}}, reserve={"rows": rows})
    cost.require_rows(ep, settlement.request_evidence([], b'{"limit": 5}'), b'{"limit": 5}')


def test_treg_key_refuses_a_plugin_request_priced_above_the_max(monkeypatch):
    module = types.ModuleType("treg.domain.plugins.demo")
    module.hold = lambda request: request.body["n"]
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    ep = row(price={"value": "1", "currency": "credit"}, reserve={"plugin": "demo.hold", "max": "5"})
    cost.require_plugin_max(ep, Rates(), settlement.request_evidence([], b'{"n": 5}'))
    with pytest.raises(ResolutionFailed) as exc:
        cost.require_plugin_max(ep, Rates(), settlement.request_evidence([], b'{"n": 6}'))
    assert exc.value.status_code == 400 and exc.value.detail["error"] == "catalog_parameter_invalid"
    cost.require_plugin_max(row(), Rates(), settlement.request_evidence([], b"{}"))  # no plugin: no check


@pytest.mark.parametrize("reported,answer,headers,actual", [
    ({"header": "x-cost", "unit": "credit", "missing": 0}, {}, {}, 0),
    ({"header": "x-cost", "unit": "credit", "missing": 0}, {}, {"x-cost": "3"}, 60000),
    ({"header": "x-cost", "unit": "credit", "missing": 0}, {}, {"x-cost": "bad"}, 20000),  # unreadable: the hold
    ({"header": "x-cost", "unit": "credit"}, {}, {}, 20000),            # without missing: the hold
    ({"path": "response.used", "unit": "credit", "missing": 0}, {}, None, 0),
    ({"path": "response.used", "unit": "credit", "missing": 0}, {"used": None}, None, 0),
])
def test_reported_missing_zero_settles_an_absent_figure_at_zero(reported, answer, headers, actual):
    ep = row(settle={"reported": reported}, reserve={"rows": {"param": "body.n", "default": {"value": 1, "source": "docs"}, "max": 5}},
             input_schema={"body": {"n": {"type": "integer"}}})
    _, decision = call(ep, answer=answer, headers=headers)
    assert decision.actual_micro == actual


@pytest.mark.parametrize("reported,answer,actual", [
    ({"header": "x-cost", "unit": "credit", "missing": 0}, {"items": [1]}, 20000),  # unknown: the hold
    ({"header": "x-cost", "unit": "credit", "missing": 0}, {"items": []}, 0),       # a free miss stays free
    ({"header": "x-cost", "unit": "credit"}, {"items": [1]}, 20000),
    ({"path": "response.used", "unit": "credit", "missing": 0}, {"used": 3}, 60000),  # the body is stored
    ({"path": "response.used", "unit": "credit", "missing": 0}, {}, 0),
])
def test_an_archive_hit_has_no_header_figure_so_missing_zero_does_not_apply(reported, answer, actual):
    ep = row(settle={"reported": reported}, miss={"when": {"path": "response.items", "op": "empty"}})
    served = {"x-cost": "3", "x-treg-cache": "hit"}  # whatever headers a hit carries, none is the vendor's
    assert call(ep, answer=answer, headers=served, cached_hit=True)[1].actual_micro == actual
    rejected = call(ep, status=400, answer=answer, headers=served, cached_hit=True)[1]
    assert rejected.billable is ("path" in reported and "used" in answer)
    assert call(ep, status=400, answer=answer, headers=served)[1].billable is ("used" in answer or "header" in reported)


@pytest.mark.parametrize("stored,actual", [
    ({"x-cost": "3"}, 60000),          # the stored figure, read as the live answer read it
    ({"x-cost": None}, 0),             # stored as absent: missing: 0 applies, as it did live
    ({"x-cost": "bad"}, 20000),        # unreadable: the outcome, then the hold
    ({"x-other": "3"}, 20000),         # stored for another header: unknown, the hold
    (None, 20000),                     # nothing stored: unknown, the hold
])
def test_an_archive_hit_settles_from_the_header_stored_with_its_answer(stored, actual):
    ep = row(settle={"reported": {"header": "X-Cost", "unit": "credit", "missing": 0}})
    live = httpx.Headers({"x-cost": "9"})
    hit = call(ep, answer={"items": [1]}, headers=live, cached_hit=True, cached_headers=stored)[1]
    assert hit.actual_micro == actual
    assert call(ep, answer={"items": [1]}, headers=live, cached_headers={"x-cost": "3"})[1].actual_micro \
        == 180000                       # a live answer reads its own header, never a stored one


# -- a spend cap the caller names, a fixed base, and a bill near the hold ------------------------

CAP = {"queryParams": {"cap": {"type": "number"}}}


def capped(**keys) -> dict:
    keys.setdefault("price", {"value": "0.001", "base": "0.0005"})
    keys.setdefault("reserve", {"money": {"param": "query.cap", "max": "1"}})
    keys.setdefault("settle", {"count": {"path": "response"}})
    return row(CAP, **keys)


def test_a_bare_top_level_array_is_counted():
    ep = row(price={"value": "0.001"}, reserve={"rows": {"param": "body.n", "default": {"value": 5, "source": "docs"},
                                                        "max": 5}},
             settle={"count": {"path": "response"}})
    assert call(ep, answer=[{"a": 1}, {"a": 2}])[1].actual_micro == 2000
    assert call(ep, answer=[])[1].actual_micro == 0
    assert call(ep, answer={"items": [1]})[1].actual_micro == 5000      # not a list: the hold


def test_a_money_hold_is_the_request_s_spend_cap_plus_one_base():
    ep = capped()
    assert call(ep, query=[("cap", "0.05")])[0] == 50500
    assert call(ep, query=[("cap", "0.0500001")])[0] == 50501             # rounded up
    for bad in ([], [("cap", "0")], [("cap", "1.5")], [("cap", "1e-2")], [("cap", "0.1"), ("cap", "0.2")]):
        assert call(ep, query=bad)[0] == 1000500                          # unreadable: the row's max
    assert ep["cost"]["type"] == "per_result" and ep["cost"]["call_fee"] == 0.0005


def test_a_base_is_billed_on_top_of_the_counted_units_and_on_a_charged_miss():
    ep = capped(miss={"when": {"path": "response", "op": "empty"}}, bill={"miss": "charged"})
    free = capped(miss={"when": {"path": "response", "op": "empty"}})
    assert call(ep, query=[("cap", "0.05")], answer=[1, 2, 3])[1].actual_micro == 3500
    assert call(ep, query=[("cap", "0.05")], answer=[])[1].actual_micro == 500
    assert call(free, query=[("cap", "0.05")], answer=[])[1].actual_micro == 0
    assert call(ep, query=[("cap", "0.05")], answer=b"")[1].actual_micro == 50500   # no evidence: the hold
    units = capped(miss={"when": {"path": "response", "op": "empty"}}, bill={"miss": 1})
    assert call(units, query=[("cap", "0.05")], answer=[])[1].actual_micro == 1500  # a fixed-unit miss carries the base


def test_a_bill_within_the_declared_units_of_the_hold_is_the_hold():
    ep = capped(settle={"count": {"path": "response"}, "hold_within_units": 2})
    query = [("cap", "0.01")]                                             # hold 10,500
    assert call(ep, query=query, answer=[1] * 7)[1].actual_micro == 7500
    assert call(ep, query=query, answer=[1] * 8)[1].actual_micro == 10500  # 8,500 + 2 units reaches it
    assert call(ep, query=query, answer=[1] * 30)[1].actual_micro == 10500  # never above the hold
    assert call(capped(settle={"count": {"path": "response"}, "hold_within_units": 20}),
                query=query, answer=[])[1].actual_micro == 500             # no row: the base, not the hold
    plain = capped()
    assert call(plain, query=query, answer=[1] * 8)[1].actual_micro == 8500
    with pytest.raises(ValueError, match="hold_within_units belongs beside"):
        row(settle={"reported": {"path": "response.cost", "unit": "usd"}, "hold_within_units": 2})


def test_a_settle_plugin_may_bill_the_base_more_than_once(monkeypatch):
    module = types.ModuleType("treg.domain.plugins.demo")
    module.runs = lambda request, answer: {"units": len(answer.body), "base": request.body["runs"]}
    module.bad = lambda request, answer: {"units": 1}
    monkeypatch.setitem(sys.modules, "treg.domain.plugins.demo", module)
    ep = capped(settle={"plugin": "demo.runs", "hold_within_units": 2}, bill={"miss": "charged"})
    query = [("cap", "0.01")]
    assert call(ep, {"runs": 3}, query=query, answer=[1, 2])[1].actual_micro == 3500
    assert call(ep, {"runs": 16}, query=query, answer=[])[1].actual_micro == 8000
    assert call(ep, {"runs": 17}, query=query, answer=[])[1].actual_micro == 8500    # no row: never the hold
    assert call(ep, {"runs": 16}, query=query, answer=[1])[1].actual_micro == 10500  # 8,500 + 2 units
    bad = capped(settle={"plugin": "demo.bad"})
    assert call(bad, {"runs": 1}, query=query, answer=[1])[1].actual_micro == 10500  # not a quantity
    with pytest.raises(ValueError, match="price.base needs a unit price"):
        row(price={"rates": {"r": "1"}, "base": "1", "currency": "USD"},
            reserve={"plugin": "demo.runs", "max": "1"})


def test_treg_key_refuses_a_money_row_without_a_spend_cap_from_its_floor_to_its_max():
    ep = capped(settle={"count": {"path": "response"}, "hold_within_units": 2})
    assert cost.money_floor_micro(ep, Rates()) == 3500                   # the base and three units
    assert cost.money_floor_micro(capped(), Rates()) == 1
    cost.require_money(ep, Rates(), settlement.request_evidence([("cap", "0.0035")], b""))
    for bad in ([], [("cap", "0.0034")], [("cap", "2")], [("cap", "-1")]):
        with pytest.raises(ResolutionFailed) as exc:
            cost.require_money(ep, Rates(), settlement.request_evidence(bad, b""))
        assert exc.value.status_code == 400 and exc.value.detail["parameter"] == "query.cap"
