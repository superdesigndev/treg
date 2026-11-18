"""Scrappa on treg's own key: every listed route is platform-priced from the credit rate in fx.yaml,
and every call settles at the credits Scrappa reports in X-Credits-Charged (0 for a free or failed
request). Prices and the meter were observed live 2026-11-18; see src/treg/catalog/scrappa.yaml."""
from __future__ import annotations

import json

import pytest

from treg.application.call import cost
from treg.application.call.resolve import MarketplaceCall
from treg.application.call.settle import decide_settle
from treg.domain.catalog import store as cs
from treg.domain.money import settlement

CREDIT_USD = 0.0003031  # fx.yaml credit_rates_usd.scrappa: $10 / 33,000 credits, rounded up


class _Rates:
    credit_rates = {"scrappa": CREDIT_USD}
    unit_rates: dict = {}
    fx = {"USD": 1.0}


def _endpoints():
    """The curated core file; the generated extended tier carries credit prices of 1 to 20."""
    return [e for e in cs.load().endpoints if e["provider"] == "scrappa" and e.get("tier") == "core"]


def test_every_scrappa_route_is_platform_priced():
    cat = cs.load()
    eps = _endpoints()
    assert len(eps) >= 10
    for ep in eps:
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "scrappa")["usd"] == pytest.approx(CREDIT_USD), ep["id"]


def _settle(ep, *, status=200, headers=None, query=None):
    request = settlement.request_evidence(query or [], b"")
    hold, unit, credit, terms = cost.price(ep, _Rates(), request)
    basis = cost.basis(_Rates(), ep, hold, terminal=False)
    mk = MarketplaceCall(tool=None, upstream="", consumed=set(), endpoint_id=ep["id"], provider="scrappa",
                         tier="platform", estimate_micro=hold, unit_micro=unit,
                         reported_charge_unit_micro=credit, settlement_basis=basis, request_data=request,
                         cost_terms=terms, call_id="c", deferred=None,
                         cost_type=(ep.get("cost") or {}).get("type") or "")
    return hold, decide_settle(mk, status, json.dumps({"items": []}).encode(), headers=headers)


@pytest.mark.parametrize("charged, credits", [("1", 1), ("2", 2), ("0", 0)])
def test_a_call_settles_at_the_credits_scrappa_reports(charged, credits):
    ep = cs.load().by_id["scrappa.google.maps.reviews"]
    hold, settled = _settle(ep, headers={"x-credits-charged": charged})
    assert hold == pytest.approx(CREDIT_USD * 1e6, abs=1)
    assert settled.actual_micro == pytest.approx(credits * CREDIT_USD * 1e6, abs=1)


def test_a_failed_request_is_free():
    ep = cs.load().by_id["scrappa.trustpilot.business.reviews"]
    decision = _settle(ep, status=404, headers={"x-credits-charged": "0"})[1]
    assert not decision.billable and decision.charged_micro == 0
