from treg import oauth_providers as P


def test_search1api_probe_is_usage():
    p = P.get("search1api")
    assert p is not None
    assert p.auth_kind == "key"
    assert p.category == "SEO"
    assert p.base_url == "https://api.search1api.com"
    assert p.probe_path == "/usage"
    assert p.token_header == "Authorization"
    assert p.token_format == "Bearer {secret}"
