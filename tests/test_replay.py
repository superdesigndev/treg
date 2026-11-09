"""The replay engine and the three seams it calls (docs/context/architecture/replay.md)."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

from treg.application.call import resolve as call_resolve
from treg.application.call import route as call_route
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall, QueryValues, price_call
from treg.application.call.route import child_outcome
from treg.application.call.settle import decide_settle
from treg.application.replay import __main__ as replay_cli
from treg.application.replay import differ, engine, fixtures, records
from treg.config import get_settings
from treg.domain import money as ledger

PER_CALL = {"id": "acme.lookup", "provider": "acme",
            "cost": {"type": "per_call", "value": 0.01, "currency": "USD", "confidence": "documented"}}
PER_RESULT = {"id": "acme.rows", "provider": "acme",
              "cost": {"type": "per_result", "value": 0.002, "currency": "USD", "confidence": "documented"}}
TABLE = {"id": "acme.crawl", "provider": "acme",
         "input": {"bodyType": "json",
                   "body": {"maxPages": {"type": "integer", "required": True, "min": 1, "max": 10}}},
         "cost": {"type": "per_success", "currency": "USD", "confidence": "documented",
                  "table": [{"when": {"body.maxPages": 2}, "value": 0.001, "times": "body.maxPages"}],
                  "fallback": {"value": 0.01}}}


def _call(ep: dict, *, cost_type: str, query=(), body: bytes = b"", consumed=frozenset(),
          deferred: list | None = None) -> MarketplaceCall:
    pricing = price_call(ep, QueryValues(tuple(query)), body, set(consumed))
    return MarketplaceCall(
        tool=None, upstream="", consumed=set(consumed), endpoint_id=ep["id"], provider=ep["provider"],
        tier="platform", cost_type=cost_type, estimate_micro=pricing.estimate_micro,
        unit_micro=pricing.unit_micro, settlement_basis=pricing.settlement_basis,
        request_data=pricing.request_data, call_id="c1", payer_org_id=1,
        reserved_micro=pricing.estimate_micro, deferred=deferred)


# ---- price_call ------------------------------------------------------------------------------

def test_price_call_fixed_price_reserves_the_price_and_settles_on_observation():
    pricing = price_call(PER_CALL, QueryValues((("q", "x"),)), b"", set())
    assert (pricing.estimate_micro, pricing.unit_micro, pricing.reported_charge_unit_micro) == (10_000, 0, 0)
    assert pricing.settlement_basis == {"when": "response", "amount": {"kind": "observed"},
                                        "fallback_micro": 10_000, "reserve_micro": 10_000}
    assert pricing.request_data == {"queryParams": {"q": "x"}, "pathParams": {}, "body": {}}


def test_price_call_reads_path_params_from_the_query_and_a_row_limit():
    pricing = price_call(PER_RESULT, QueryValues((("limit", "5"), ("id", "42"))), b"", {"id"})
    assert (pricing.estimate_micro, pricing.unit_micro) == (10_000, 2_000)
    assert pricing.request_data == {"queryParams": {"limit": "5"}, "pathParams": {"id": "42"}, "body": {}}


def test_price_call_prices_a_table_from_the_request_body():
    pricing = price_call(TABLE, QueryValues(()), b'{"maxPages": 2}', set())
    assert pricing.estimate_micro == 2_000
    assert pricing.settlement_basis["amount"]["kind"] == "table"
    assert (pricing.settlement_basis["reserve_micro"], pricing.settlement_basis["fallback_micro"]) == (2_000, 10_000)


# ---- decide_settle ---------------------------------------------------------------------------

def test_decide_settle_charges_a_served_call_its_basis():
    decision = decide_settle(_call(PER_CALL, cost_type="per_call"), 200, b'{"ok": true}')
    assert (decision.billable, decision.reason, decision.observed_micro, decision.actual_micro) == (
        True, "", None, 10_000)
    assert decision.charged_micro == ledger.with_margin(10_000)


def test_decide_settle_releases_a_rejected_request_the_provider_did_not_bill():
    decision = decide_settle(_call(PER_CALL, cost_type="per_call"), 400, b'{"error": "bad"}')
    assert (decision.billable, decision.reason, decision.actual_micro, decision.charged_micro) == (
        False, "rejected_unbilled_400", None, 0)
    assert decide_settle(_call(PER_CALL, cost_type="per_call"), 401, b"").billable is False
    assert decide_settle(_call(PER_CALL, cost_type="per_call"), None).billable is False


def test_decide_settle_applies_the_repeat_price_and_the_routed_child_free_rule():
    repeat = decide_settle(_call(PER_CALL, cost_type="per_call"), 200, b"{}", cached_repeat=True)
    assert repeat.actual_micro == 10_000 * get_settings().archive_hit_repeat_price_percent // 100
    free = decide_settle(_call(PER_CALL, cost_type="per_call", deferred=[]), 200, b"{}",
                         observed_override=0)
    assert (free.billable, free.reason, free.charged_micro) == (False, "free_answer", 0)


async def test_platform_settle_hands_a_routed_child_the_decision():
    """The deferred branch writes nothing to the ledger, so the call site is checked directly."""
    pending: list = []
    mk = _call(PER_CALL, cost_type="per_call", deferred=pending)
    expected = decide_settle(_call(PER_CALL, cost_type="per_call", deferred=[]), 200, b"{}")
    charged, observed = await call_settle._platform_settle(mk, 200, b"{}")
    assert (charged, observed) == (expected.charged_micro, expected.observed_micro)
    assert [(p.billable, p.actual_micro) for p in pending] == [(expected.billable, expected.actual_micro)]
    assert mk.call_id is None


# ---- child_outcome ---------------------------------------------------------------------------

class _Adapter:
    def __init__(self, *, miss: bool = False, core: dict | None = None, boom: str = ""):
        self._miss, self._core, self._boom = miss, core if core is not None else {"email": "a@b.co"}, boom

    def from_upstream(self, doc):
        if self._boom == "from_upstream":
            raise KeyError("x")
        return self._core

    def is_miss(self, doc):
        if self._boom == "is_miss":
            raise KeyError("x")
        return self._miss


CONTRACT = SimpleNamespace(required_output=("email",))
CHILD = {"id": "acme.email.find", "provider": "acme", "miss": {"status": 404}}


@pytest.mark.parametrize(("status", "raw", "adapter", "tier", "kind", "why"), [
    (404, b"{}", _Adapter(), "platform", "declared_miss", ()),
    (400, b"{}", _Adapter(), "platform", "rejected", ()),
    (401, b"{}", _Adapter(), "platform", "error", ()),
    (401, b"{}", _Adapter(), "credential", "rejected", ()),
    (429, b"{}", _Adapter(), "platform", "error", ()),
    (500, b"{}", _Adapter(), "platform", "error", ()),
    (200, b"{}", _Adapter(boom="from_upstream"), "platform", "adapter_error", ()),
    (200, b"{}", _Adapter(boom="is_miss"), "platform", "adapter_error", ()),
    (200, b"<html>", _Adapter(), "platform", "miss", ("non_json", "adapter_miss", "empty_core")),
    (200, b"{}", _Adapter(miss=True), "platform", "miss", ("adapter_miss",)),
    (200, b"{}", _Adapter(core={"email": ""}), "platform", "miss", ("empty_core",)),
    (200, b"{}", _Adapter(core={"email": "a@b.co", "rows": [1, 2, 3]}), "platform", "hit", ()),
])
def test_child_outcome_classifies_each_kind_of_answer(status, raw, adapter, tier, kind, why):
    outcome = child_outcome(CHILD, CONTRACT, adapter, status, raw, tier=tier)
    assert (outcome.kind, outcome.why) == (kind, why)
    if kind == "hit":
        assert outcome.rows == 3


def test_child_outcome_reads_a_declared_not_found_before_the_status_rules():
    ep = {**CHILD, "not_found": {"status": [404, 410]}, "miss": None}
    assert child_outcome(ep, CONTRACT, _Adapter(), 410, b"{}", tier="platform").kind == "not_found"


# ---- the engine ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    return list(fixtures.generate())


@pytest.fixture
def offline(monkeypatch):
    """No socket and no database session may be opened while a corpus replays."""
    def refuse(*args, **kwargs):
        raise AssertionError("a replay opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    for module in (call_resolve, call_settle, call_route):
        monkeypatch.setattr(module, "session_maker", refuse)


def test_every_fixture_record_replays_offline_with_no_failed_step(corpus, offline):
    assert {r["class"] for r in corpus} == set(records.CLASSES)
    lines = [engine.replay(r) for r in corpus]
    failed = sorted({(line["endpoint_id"], group) for line in lines for group in engine.GROUPS
                     if isinstance(line[group], dict) and "error" in line[group]})
    assert failed == []
    assert not [line for line in lines if line.get("error")]
    assert [engine.replay(r) for r in corpus[:200]] == lines[:200]


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_a_replay_line_holds_no_record_content(corpus):
    leaked = []
    for record in corpus:
        text = json.dumps(engine.replay(record))
        body = json.loads(records.decode(record["response"]["body_b64"]) or b"null")
        # A body may name its own endpoint ("autocomplete"); only other text counts.
        leaked += [record["rid"] for s in _strings(body)
                   if len(s) >= 12 and s not in record["rid"] and s in text]
    assert leaked == []


def _settles_at_its_estimate(line: dict) -> bool:
    settle, reserve = line["settle"] or {}, line["reserve"] or {}
    return (settle.get("billable") is True and settle.get("observed_micro") is None
            and settle.get("actual_micro") == reserve.get("estimate_micro") > 0)


def test_a_catalog_price_change_shows_as_a_reserve_and_settle_difference(corpus, monkeypatch):
    record = next(r for r in corpus if r["rid"].endswith(":example")
                  and _settles_at_its_estimate(engine.replay(r))
                  and isinstance(engine.catalog_store.load().by_id[r["endpoint_id"]]["cost"].get("value"), float))
    baseline = {record["rid"]: engine.replay(record)}
    ep = engine.catalog_store.load().by_id[record["endpoint_id"]]
    monkeypatch.setitem(ep, "cost", {**ep["cost"], "value": ep["cost"]["value"] * 2})
    candidate = {record["rid"]: engine.replay(record)}
    report = differ.compare(baseline, candidate, [])
    assert not report.passed
    assert {"reserve.estimate_micro", "settle.actual_micro"} <= {c.field for c in report.undeclared}
    declared = differ.parse_declarations([
        {"provider": ep["provider"], "field": "reserve.*", "reason": "price doubled"},
        {"endpoint": ep["id"], "field": "settle.*_micro", "reason": "price doubled"},
        {"endpoint": "nobody.*", "field": "outcome.hit", "to": False, "reason": "unused"},
    ])
    report = differ.compare(baseline, candidate, declared)
    assert report.passed
    assert [d.reason for d in report.untested_declarations] == ["unused"]


# ---- the differ ------------------------------------------------------------------------------

def _line(rid: str, **groups) -> dict:
    return {"rid": rid, "endpoint_id": "acme.lookup", "provider": "acme", "class": "hit",
            **dict.fromkeys(engine.GROUPS), **groups}


def test_a_declaration_matches_on_from_to_and_where():
    base = {"r1": _line("r1", settle={"actual_micro": 4000}, outcome={"state": "error"}),
            "r2": _line("r2", settle={"actual_micro": 4000}, outcome={"state": "found"})}
    cand = {"r1": _line("r1", settle={"actual_micro": 0}, outcome={"state": "error"}),
            "r2": _line("r2", settle={"actual_micro": 0}, outcome={"state": "found"})}
    rule = differ.parse_declarations([{"endpoint": "acme.lookup", "field": "settle.actual_micro",
                                       "from": 4000, "to": 0, "where": {"outcome.state": "error"},
                                       "reason": "envelope"}])
    report = differ.compare(base, cand, rule)
    assert [c.rid for c in report.undeclared] == ["r2"]
    assert not report.passed


def test_a_declaration_matches_a_list_valued_field():
    base = {"r1": _line("r1", routing={"why": ["empty_core"]})}
    cand = {"r1": _line("r1", routing={"why": ["adapter_miss"]})}
    rule = differ.parse_declarations([{"endpoint": "acme.lookup", "field": "routing.why",
                                       "from": ["empty_core"], "to": ["adapter_miss"],
                                       "reason": "stricter adapter"}])
    report = differ.compare(base, cand, rule)
    assert report.passed and not report.untested_declarations


@pytest.mark.parametrize(("old", "new", "declared", "covered"), [
    (False, True, {"from": 0}, False),
    (False, True, {"to": 1}, False),
    (0, 1, {"from": False}, False),
    (0, 5, {"from": None}, False),
    ("", "x", {"from": None}, False),
    (None, 5, {"from": 0}, False),
    (None, "x", {"from": ""}, False),
    ("1000", 0, {"from": 1000}, False),
    (1000, 0, {"from": "1000"}, False),
    (["0"], ["1"], {"from": [0]}, False),
    (False, True, {"from": False, "to": True}, True),
    (1000, 0, {"from": 1000.0, "to": 0}, True),
    (1000.0, 0.0, {"from": 1000, "to": 0}, True),
    (None, 5, {"from": None, "to": 5}, True),
    ("a", "b", {"from": "a", "to": "b"}, True),
    (["a"], ["b"], {"from": ["a"], "to": ["b"]}, True),
])
def test_a_declared_value_matches_only_a_value_of_its_own_kind(old, new, declared, covered):
    base = {"r1": _line("r1", settle={"billable": old})}
    cand = {"r1": _line("r1", settle={"billable": new})}
    rule = differ.parse_declarations([{"endpoint": "acme.lookup", "field": "settle.billable",
                                       **declared, "reason": "kind"}])
    assert differ.compare(base, cand, rule).passed is covered


@pytest.mark.parametrize(("state", "where", "covered"), [
    (False, False, True), (False, 0, False), (0, False, False), (0, 0.0, True),
    (None, None, True), (None, 0, False), ("", None, False), ("1", 1, False),
])
def test_a_where_value_matches_only_a_value_of_its_own_kind(state, where, covered):
    base = {"r1": _line("r1", settle={"actual_micro": 4000}, outcome={"flag": state})}
    cand = {"r1": _line("r1", settle={"actual_micro": 0}, outcome={"flag": state})}
    rule = differ.parse_declarations([{"endpoint": "acme.lookup", "field": "settle.actual_micro",
                                       "where": {"outcome.flag": where}, "reason": "kind"}])
    assert differ.compare(base, cand, rule).passed is covered


def test_every_declaration_a_change_matches_is_exercised():
    base = {"r1": _line("r1", settle={"actual_micro": 4000})}
    cand = {"r1": _line("r1", settle={"actual_micro": 0})}
    broad, narrow = differ.parse_declarations([
        {"provider": "acme", "field": "settle.*", "reason": "broad"},
        {"endpoint": "acme.lookup", "field": "settle.actual_micro", "to": 0, "reason": "narrow"}])
    report = differ.compare(base, cand, [broad, narrow])
    assert report.passed and not report.untested_declarations
    assert [declared for _, declared in report.changes] == [broad]


def test_the_report_shows_amounts_and_codes_never_text():
    base = {"r1": _line("r1", settle={"actual_micro": 4000, "reason": "a free-text reason"})}
    cand = {"r1": _line("r1", settle={"actual_micro": 0, "reason": "rejected_unbilled_400"})}
    text = differ.render(differ.compare(base, cand, []))
    assert "settle.actual_micro 4000 -> 0" in text
    assert "a free-text reason" not in text and "| NO |" in text


def test_reason_only_changes_are_listed_apart_and_touched_endpoints_are_counted():
    base = {"r1": _line("r1", settle={"reason": "x"})}
    cand = {"r1": _line("r1", settle={"reason": "y"})}
    report = differ.compare(base, cand, [], base_fingerprints={"acme.lookup": "1", "acme.gone": "1"},
                            candidate_fingerprints={"acme.lookup": "2"})
    text = differ.render(report)
    assert "### Reason-code-only changes" in text and "### Changed records" not in text
    assert report.touched == {"acme.lookup": {"hit": 1}, "acme.gone": {}}
    assert "- acme.gone: untested" in text


def test_a_record_missing_from_one_side_fails_the_diff():
    report = differ.compare({"r1": _line("r1")}, {}, [])
    assert report.missing_in_candidate == 1 and not report.passed


@pytest.mark.parametrize("raw", [
    [{"field": "settle.*", "reason": "x"}],
    [{"endpoint": "a", "provider": "b", "reason": "x"}],
    [{"endpoint": "a"}],
    [{"endpoint": "a", "reason": "x", "colour": "red"}],
])
def test_a_malformed_declaration_is_refused(raw):
    with pytest.raises(ValueError):
        differ.parse_declarations(raw)


def test_the_cli_reads_declarations_from_a_markdown_block(tmp_path, capsys):
    base, cand = tmp_path / "base.jsonl", tmp_path / "cand.jsonl"
    records.write(base, [_line("r1", reserve={"estimate_micro": 1})])
    records.write(cand, [_line("r1", reserve={"estimate_micro": 2})])
    assert replay_cli.main(["diff", str(base), str(cand)]) == 1
    body = tmp_path / "pr.md"
    body.write_text("Price fix.\n\n```replay-declared\n- endpoint: acme.lookup\n"
                    "  field: reserve.estimate_micro\n  reason: new price\n```\n")
    assert replay_cli.main(["diff", str(base), str(cand), "--declared-in", str(body)]) == 0
    assert "new price" in capsys.readouterr().out


def test_a_line_is_the_compact_sorted_json_write_puts_on_each_row(tmp_path):
    row = {"rid": "r1", "endpoint_id": "e", "b": 1, "a": [1, 2]}
    assert records.line(row) == '{"a":[1,2],"b":1,"endpoint_id":"e","rid":"r1"}'
    assert records.write(tmp_path / "x.jsonl", [row]) == 1
    assert (tmp_path / "x.jsonl").read_text() == records.line(row) + "\n"
