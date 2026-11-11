"""Deeplead: every paid route runs on treg's partner key, priced in USD per call and metered in the
x-deeplead-cost-usd response header; see src/treg/catalog/deeplead.yaml."""
from __future__ import annotations

from treg.domain.catalog import store as cs


def test_every_paid_deeplead_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "deeplead"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "deeplead")["usd"] > 0, ep["id"]
