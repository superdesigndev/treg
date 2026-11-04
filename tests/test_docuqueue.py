"""DocuQueue: 1 credit = 1 document (fx.yaml: Starter $9 / 250 docs = $0.036 per credit).
Every paid route bills document credits; lists, previews, status and download are free."""
from __future__ import annotations

from treg.domain.catalog import store as cs


def test_every_docuqueue_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "docuqueue"]
    assert eps, "the docuqueue catalog file must load"
    for ep in eps:
        assert cat.platform_eligible(ep), ep["id"]
        if ep["cost"]["type"] != "free":
            assert cat.cost_view(ep["cost"], "docuqueue")["usd"] > 0, ep["id"]
