"""Tokfluence listing: every data endpoint is priced in credits that fx.yaml converts to USD, so the
file is platform-eligible end to end. The free /v1/lists pre-flight is the caller's own account
(own_account), which treg never serves on a platform key by design."""
from treg import oauth_providers as P
from treg.domain.catalog import store as cs


def test_tokfluence_is_a_registered_key_provider():
    p = P.REGISTRY["tokfluence"]
    assert p.auth_kind == "key"
    assert p.token_header == "Authorization" and p.token_format == "Bearer {secret}"
    assert p.probe_path == "/v1/lists"


def test_tokfluence_catalog_is_platform_priced():
    c = cs.load()
    eps = [ep for ep in c.by_id.values() if ep.get("provider") == "tokfluence"]
    assert 8 <= len(eps) <= 15
    for ep in eps:
        if ep.get("scope") == "own_account":
            assert not c.platform_eligible(ep), ep["id"]
            continue
        assert c.platform_eligible(ep), ep["id"]
