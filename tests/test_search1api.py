"""Search1API bring-your-own-key catalog: fixed credit rows and a free usage probe."""

from __future__ import annotations

from treg.domain.catalog.store import load
from treg import oauth_providers as P


def test_search1api_connects_with_a_free_bearer_probe():
    provider = P.REGISTRY["search1api"]
    assert provider.auth_kind == "key"
    assert provider.uses_pasted_secret is True
    assert P.is_configured(provider) is True
    assert provider.base_url == "https://api.search1api.com"
    assert provider.token_header == "Authorization"
    assert provider.token_format == "Bearer {secret}"
    assert provider.probe_path == "/usage"
    assert provider.probe_method == "GET"
    assert provider.probe_cost_micro == 0


def test_search1api_rows_are_platform_priced_from_the_entry_topup():
    catalog = load()
    expected = {
        "search1api.web.search": 0.001,
        "search1api.web.search.news": 0.001,
        "search1api.web.search.publications": 0.001,
        "search1api.web.crawl": 0.001,
        "search1api.web.extract": 0.010,
        "search1api.web.map": 0.001,
        "search1api.web.screenshot": 0.002,
        "search1api.github.trending.repositories": 0.001,
        "search1api.web.trending.hackernews": 0.001,
    }
    search = catalog.by_id["search1api.web.search"]
    assert search["input"]["body"]["crawl_results"]["enum"] == [0]
    assert search["input"]["body"]["image"]["enum"] == [False]
    for endpoint_id, usd in expected.items():
        row = catalog.by_id[endpoint_id]
        assert catalog.platform_eligible(row), endpoint_id
        view = catalog.cost_view(row["cost"], "search1api")
        assert view["usd"] == usd
        assert row["cost"]["confidence"] in {"verified", "documented"}
