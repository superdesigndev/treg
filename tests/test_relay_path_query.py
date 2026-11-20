"""The relay keeps a catalog path's own query string when merging the caller's params.

httpx replaces a URL's existing query whenever `params=` is given, even an empty list, so
`/rest/images?action=initializeUpload` reached LinkedIn as `/rest/images`. The relay now merges
the caller's pairs onto the URL, the same way the health probe does.
"""

from __future__ import annotations

import httpx
import pytest

from treg.application.call.types import UpstreamRequest
from treg.infra.upstream import relay as relay_module
from treg.models import Tool


async def _relayed_url(monkeypatch, query_items) -> httpx.URL:
    seen: dict[str, httpx.URL] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = req.url
        return httpx.Response(200, content=b"ok")

    tool = Tool(org_id=1, name="echo", owner="t", base_url="https://echo.example",
                host="echo.example", bindings=[])
    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", False, raising=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        req = UpstreamRequest(method="POST", raw_headers=(), query_items=query_items,
                              body_stream=None, has_body=False)
        resp = await relay_module.relay(
            req, "https://echo.example/rest/images?action=initializeUpload", tool, {}, client)
        await resp.close()
    return seen["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("query_items", "expected"), [
    ((), [("action", "initializeUpload")]),
    ((("tag", "a"), ("tag", "b")), [("action", "initializeUpload"), ("tag", "a"), ("tag", "b")]),
])
async def test_caller_params_merge_onto_path_query_keeping_duplicates(
    monkeypatch, query_items, expected,
) -> None:
    url = await _relayed_url(monkeypatch, query_items)
    assert url.params.multi_items() == expected
