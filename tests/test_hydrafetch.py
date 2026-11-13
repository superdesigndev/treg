from treg.domain.catalog import store as cs


def test_every_paid_hydrafetch_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "hydrafetch"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free" or ep.get("scope") == "own_account":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "hydrafetch")["usd"] > 0, ep["id"]
