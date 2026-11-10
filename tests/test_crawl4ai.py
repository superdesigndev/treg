"""Crawl4AI Cloud: every paid route runs on treg's key and settles at the charge Crawl4AI reports
(x-c4-cost on a single answer, the closing summary line of a batch stream, a finished job's
usage.cost). Prices and wire shapes observed live 2026-10-05; see src/treg/catalog/crawl4ai.yaml."""
from __future__ import annotations

import json

import pytest

from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.domain.capacity import signatures as S
from treg.domain.catalog import store as cs
from treg.domain.money import settlement as settlement_basis


def test_every_paid_crawl4ai_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "crawl4ai"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free" or ep.get("scope") == "own_account":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "crawl4ai")["usd"] > 0, ep["id"]


def _mk(endpoint_id: str, unit_micro: int = 2_000) -> MarketplaceCall:
    return MarketplaceCall(
        tool=None, upstream="https://api.crawl4ai.com/scrape", consumed=set(), provider="crawl4ai",
        endpoint_id=endpoint_id, tier="platform", estimate_micro=unit_micro, cost_type="per_success",
        unit_micro=unit_micro, reported_charge_unit_micro=1_000, request_data={},
    )


@pytest.mark.parametrize("header, micro", [("0.250", 250), ("1.952", 1_952), ("2.000", 2_000)])
def test_a_single_answer_settles_at_its_x_c4_cost_header(header, micro):
    """1 credit = $0.001 (fx.yaml), so a 0.25-credit archived scrape is 250 micro-USD."""
    mk = _mk("crawl4ai.web.scrape")
    assert call_settle._observed_cost_micro(mk, b'{"ok": true}', headers={"x-c4-cost": header}) == micro


def test_a_batch_stream_settles_at_its_closing_summary_line():
    """No header can carry a streamed total; the stream's last line does. `_usage_document` reads
    an NDJSON body's last line, and the endpoint's `usage.path` is `summary.cost`."""
    stream = b"\n".join(json.dumps(x).encode() for x in (
        {"url": "https://a.example", "ok": True, "result": {}, "cost": "0.250", "effort": 0},
        {"url": "https://b.example", "ok": True, "result": {}, "cost": "1.000", "effort": 3},
        {"summary": {"cost": "1.250", "priced": 2, "urls": 2}},
    )) + b"\n"
    doc = call_settle._usage_document(stream)
    assert doc == {"summary": {"cost": "1.250", "priced": 2, "urls": 2}}
    ep = cs.load().by_id["crawl4ai.web.extract.batch"]
    assert ep["cost"]["usage"] == {"path": "summary.cost", "unit": "credit"}
    assert call_settle._usage_document(b'{"usage": {"cost": "2.000"}}') == {"usage": {"cost": "2.000"}}
    assert call_settle._usage_document(b"") is None and call_settle._usage_document(b"not json") is None


def test_a_finished_job_settles_at_its_usage_cost():
    ep = cs.load().by_id["crawl4ai.web.scrape.job.start"]
    assert ep["cost"]["usage"] == {"path": "usage.cost", "unit": "credit"}
    assert ep["async"]["status"]["success"] == ["done"] and ep["async"]["id_from"] == "job_id"


@pytest.mark.parametrize("status, body, kind", [
    (402, b'{"error":"no_credit"}', "balance"),
    (402, b'{"error":"spend_cap"}', "quota"),
    (402, b'{"error":"plan_cap","message":"..."}', "quota"),
    (429, b'{"message":"You reached the free plan\'s limit"}', "burst"),
])
def test_crawl4ai_out_of_credit_answers_are_classified(status, body, kind):
    signal = S.classify("crawl4ai", status, {}, body)
    assert signal is not None and signal.kind == kind


@pytest.mark.parametrize("raw, value", [
    ("0.500", 0.5), ("12", 12.0), (0.25, 0.25), ("-1", None), ("1e3", None), ("abc", None), (True, None), ("", None),
])
def test_a_usage_figure_reported_as_a_decimal_string_is_read(raw, value):
    """Crawl4AI's batch summary and job usage report credits as strings ("0.500"); before this they
    read as "no usage" and the call settled at its whole hold (50 credits for a 0.5-credit batch)."""
    assert settlement_basis._usage_number(raw) == value
