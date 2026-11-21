"""Boomerang: every paid row runs on treg's key, priced in USD from Boomerang's public rate card
(GET https://app.theboomerang.co/api/v1/pricing), billed per record found. See
src/treg/catalog/boomerang.yaml."""
from __future__ import annotations

from treg.domain.catalog import store as cs


def test_every_paid_boomerang_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "boomerang"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free" or ep.get("scope") == "own_account":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "boomerang")["usd"] > 0, ep["id"]


def test_boomerang_async_rows_poll_the_status_route():
    cat = cs.load()
    eps = {e["id"]: e for e in cat.endpoints if e["provider"] == "boomerang"}
    for ep_id, ep in eps.items():
        if ep.get("kind") == "utility":
            continue
        assert ep["method"] == "POST" and ep["path"] == "/requests/single", ep_id
