"""Data Legion: both enrich routes run on treg's key at the published self-serve prices, a 404
no-match is a declared, free miss, and the phone check is free. Wire shapes observed live
2026-10-21; see src/treg/catalog/datalegion.yaml."""
from __future__ import annotations

from treg.domain.catalog import store as cs


def _endpoints():
    cat = cs.load()
    return cat, {e["id"]: e for e in cat.endpoints if e["provider"] == "datalegion"}


def test_every_paid_datalegion_route_is_platform_priced():
    cat, eps = _endpoints()
    assert eps
    for ep in eps.values():
        if ep["cost"]["type"] == "free" or ep.get("scope") == "own_account":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "datalegion")["usd"] > 0, ep["id"]


def test_datalegion_enrich_prices_and_misses():
    _, eps = _endpoints()
    assert eps["datalegion.people.enrich"]["cost"]["value"] == 0.05
    assert eps["datalegion.companies.enrich"]["cost"]["value"] == 0.01
    for endpoint_id in ("datalegion.people.enrich", "datalegion.companies.enrich"):
        assert eps[endpoint_id]["cost"]["type"] == "per_success", endpoint_id
        assert eps[endpoint_id]["miss"]["status"] == 404, endpoint_id


def test_datalegion_phone_check_is_free_and_shares_the_capability():
    _, eps = _endpoints()
    ep = eps["datalegion.people.phone.verify"]
    assert ep["capability"] == "people.phone.verify"
    assert ep["cost"]["type"] == "free"
    assert ep["path"] == "/utility/validate"
