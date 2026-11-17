"""Seltz is priced in published USD, so every listed route can run on treg's key."""

from treg.domain.catalog import store
from treg.oauth_providers import SELTZ


def test_seltz_registry_checks_the_key_in_a_header():
    assert SELTZ.auth_kind == "key"
    assert SELTZ.token_header == "x-api-key"
    assert SELTZ.token_format == "{secret}"
    assert SELTZ.base_url == "https://api.seltz.ai"
    assert SELTZ.probe_path == "/v1/monitors"
    assert SELTZ.probe_method == "GET"


def test_seltz_endpoints_are_platform_priced():
    catalog = store.load()
    rows = [endpoint for endpoint in catalog.endpoints if endpoint["provider"] == "seltz"]
    assert {endpoint["id"] for endpoint in rows} == {
        "seltz.web.search",
        "seltz.web.search.news",
        "seltz.people.search",
        "seltz.companies.search",
        "seltz.wikipedia.search",
        "seltz.stocks.filings.search",
        "seltz.web.answer",
        "seltz.web.fetch",
        "seltz.web.research",
        "seltz.web.research.status",
    }
    for endpoint in rows:
        assert catalog.platform_eligible(endpoint), endpoint["id"]
    search = catalog.by_id["seltz.web.search"]["cost"]
    by_tier = {row["when"]["body.tier"]: row["value"] for row in search["table"]}
    assert by_tier == {"base": 0.003, "pro": 0.005}
    assert search["fallback"]["value"] == 0.005
    assert search["currency"] == "USD"
