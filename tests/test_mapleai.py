"""MapleAI: every paid route is platform-priced; the free meter routes stay free.
Prices observed against the live rate card and prepaid budget 2026-10-12; see src/treg/catalog/mapleai.yaml."""
from __future__ import annotations

from treg.domain.catalog import store as cs


def test_every_paid_mapleai_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "mapleai"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free" or ep.get("scope") == "own_account":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "mapleai")["usd"] > 0, ep["id"]
