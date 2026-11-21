"""RealtyAPI: one key (x-realtyapi-key) across per-portal hosts, one credit per request, and a free
/account key check. Charges observed live 2026-11-17; see src/treg/catalog/realtyapi.yaml."""
from __future__ import annotations

from urllib.parse import urlparse

from treg import oauth_providers as P
from treg.domain.catalog import store as cs


def _rows():
    eps = [e for e in cs.load().endpoints if e["provider"] == "realtyapi"]
    assert eps
    return eps


def test_every_realtyapi_route_is_platform_priced():
    cat = cs.load()
    for ep in _rows():
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "realtyapi")["usd"] > 0, ep["id"]


def test_every_realtyapi_host_is_an_approved_target():
    provider = P.REGISTRY["realtyapi"]
    approved = {urlparse(provider.base_url).hostname} | {t.host for t in provider.catalog_targets}
    for ep in _rows():
        assert (ep.get("host") or urlparse(provider.base_url).hostname) in approved, ep["id"]


def test_realtyapi_checks_keys_on_the_free_account_route():
    provider = P.REGISTRY["realtyapi"]
    assert provider.auth_kind == "key"
    assert provider.token_header == "x-realtyapi-key"
    assert provider.token_format == "{secret}"
    assert provider.probe_path == "/account"
    assert provider.category in P.CATEGORY_ORDER


async def test_the_balance_collector_reads_the_free_account_route():
    import httpx
    from treg.domain.capacity import collectors

    seen = {}

    def account(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["key"] = str(request.url), request.headers.get("x-realtyapi-key")
        return httpx.Response(200, json={"valid": True, "creditsLimit": 20000, "creditsRemaining": 19123})

    async with httpx.AsyncClient(transport=httpx.MockTransport(account)) as client:
        got = await collectors.BALANCE_ROUTES["realtyapi"](client, "rt_test")
    assert seen == {"url": "https://zillow.realtyapi.io/account", "key": "rt_test"}
    assert got["value"] == 19123 and got["unit"] == "credits"


def test_an_empty_credit_pool_reads_as_balance():
    from treg.domain.capacity import signatures as S

    assert S.classify("realtyapi", 402, {}, b'{"error":"Credit limit exceeded"}').kind == "balance"
