"""Provider addresses never reach the log: the HTTP clients log each request's full URL at INFO,
and a query-string key (SerpAPI `api_key`, Datagma `apiId`, ...) is part of that URL."""

from __future__ import annotations

import logging

import treg.api  # noqa: F401 - builds the MCP servers, which turn the root logger on at INFO


def test_the_http_clients_log_nothing_below_warning():
    assert logging.getLogger().getEffectiveLevel() <= logging.INFO      # the reason this test exists
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


async def test_a_provider_call_writes_no_url_to_the_log(caplog):
    import httpx

    caplog.set_level(logging.INFO)
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://serpapi.com/search?q=x&api_key=SECRET123")
    assert "SECRET123" not in caplog.text
