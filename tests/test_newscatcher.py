"""NewsCatcher: every paid news and research route can run on treg's key, priced in NCC credits at
the public $25 / 25,000 top-up (fx.yaml). Prices transcribed from the published credits page; see
src/treg/catalog/newscatcher.yaml."""
from __future__ import annotations

import httpx
import pytest

from treg import oauth_providers as P
from treg.config import get_settings, platform_setting_name
from treg.domain.capacity import collectors
from treg.domain.catalog import store as cs

# Own-account reads (the balance, your runs and monitors) never run on a shared key, and company
# research is priced per company, which one async hold cannot follow.
NOT_ON_TREGS_KEY = {
    "newscatcher.account.usage",
    "newscatcher.account.runs.list",
    "newscatcher.account.monitors.list",
    "newscatcher.account.monitor.stories",
    "newscatcher.companies.news.research",
}


def _rows():
    cat = cs.load()
    return cat, [e for e in cat.endpoints if e["provider"] == "newscatcher"]


def test_every_newscatcher_route_but_own_account_and_per_company_is_platform_priced():
    cat, eps = _rows()
    assert eps
    for ep in eps:
        if ep["id"] in NOT_ON_TREGS_KEY:
            assert not cat.platform_eligible(ep), ep["id"]
            continue
        assert cat.platform_eligible(ep), ep["id"]
        if ep["cost"]["type"] != "free":
            assert cat.cost_view(ep["cost"], "newscatcher")["usd"] > 0, ep["id"]


def test_newscatcher_credits_convert_at_the_public_top_up_rate():
    cat, eps = _rows()
    by_id = {e["id"]: e for e in eps}
    assert cat.cost_view(by_id["newscatcher.web.search.news.articles"]["cost"], "newscatcher")["usd"] == 0.005
    assert cat.cost_view(by_id["newscatcher.web.news.story"]["cost"], "newscatcher")["usd"] == 0.001


def test_newscatcher_connects_with_the_raw_key_and_probes_the_free_subscription_route():
    p = P.REGISTRY["newscatcher"]
    assert (p.token_header, p.token_format) == ("x-api-key", "{secret}")
    assert p.base_url + p.probe_path == "https://api.newscatcherapi.com/news-api/subscription"


async def _balance(monkeypatch, body):
    monkeypatch.setattr(get_settings(), platform_setting_name("newscatcher"), "test")

    def probe(request):
        assert request.method == "GET"
        assert request.url == "https://api.newscatcherapi.com/news-api/subscription"
        assert request.headers["x-api-key"] == "test"
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        return await collectors.provider_balance("newscatcher", client)


async def test_newscatcher_capacity_reads_the_free_subscription_balance(monkeypatch):
    row = await _balance(monkeypatch, {"plan": "free", "credits_balance": 9871.5})
    assert (row["value"], row["unit"]) == (9871.5, "credits")


@pytest.mark.parametrize("balance", [None, True, "100", -1])
async def test_newscatcher_capacity_refuses_a_balance_it_cannot_trust(balance, monkeypatch):
    row = await _balance(monkeypatch, {"plan": "free", "credits_balance": balance})
    assert row["value"] is None and "NewsCatcher returned no valid credit balance" in row["note"], row
