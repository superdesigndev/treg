"""A `location: path` binding fills only its own `{name}` segment of the upstream path.

Forager binds each API key to one account id that every paid route carries in its path. The id is
part of the credential: on treg's key the caller cannot know it, so the relay fills it.
"""

from __future__ import annotations

import httpx
import pytest

from treg.application.call import resolve as call_resolution
from treg.application.call.types import GatewayFailed, UpstreamRequest
from treg.config import get_settings
from treg.infra.upstream import pathsegment
from treg.infra.upstream import relay as relay_module
from treg.models import Tool
from treg.oauth_providers import OAuthProvider

_BINDINGS = [
    {"platform_setting": "platform_key_test_key", "injector": "env", "location": "header",
     "name": "X-API-KEY", "format": "{secret}"},
    {"platform_setting": "platform_key_test_account", "injector": "env", "location": "path",
     "name": "account_id", "format": "{secret}"},
]


async def _relayed(monkeypatch, url: str, account: str = "1234",
                   query_items=()) -> httpx.Request:
    seen: dict[str, httpx.Request] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["req"] = req
        return httpx.Response(200, content=b"ok")

    values = {"platform_key_test_key": "KEY", "platform_key_test_account": account}
    settings = get_settings()
    monkeypatch.setattr(type(settings), "platform_credential",
                        lambda self, setting, acct: values.get(setting, ""))
    monkeypatch.setattr(settings, "proxy_ssrf_check", False, raising=False)
    tool = Tool(org_id=1, name="forager", owner="t", base_url="https://api.example",
                host="api.example", bindings=_BINDINGS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        req = UpstreamRequest(method="GET", raw_headers=(), query_items=query_items,
                              body_stream=None, has_body=False)
        resp = await relay_module.relay(req, url, tool, {}, client)
        await resp.close()
    return seen["req"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://api.example/api/{account_id}/datastorage/x/",
    "https://api.example/api/%7Baccount_id%7D/datastorage/x/",
    "https://api.example/api/%7baccount_id%7d/datastorage/x/",
])
async def test_path_credential_fills_its_placeholder(monkeypatch, url):
    req = await _relayed(monkeypatch, url)
    assert req.url.raw_path == b"/api/1234/datastorage/x/"
    assert req.headers["x-api-key"] == "KEY"


@pytest.mark.asyncio
async def test_path_credential_touches_neither_the_query_nor_other_segments(monkeypatch):
    req = await _relayed(monkeypatch, "https://api.example/api/{account_id}/x/{other}/",
                         query_items=(("q", "{account_id}"),))
    assert req.url.raw_path == b"/api/1234/x/%7Bother%7D/?q=%7Baccount_id%7D"


@pytest.mark.asyncio
async def test_a_path_without_the_placeholder_is_relayed_as_sent(monkeypatch):
    req = await _relayed(monkeypatch, "https://api.example/api/9999/datastorage/x/")
    assert req.url.raw_path == b"/api/9999/datastorage/x/"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["..", "a/b"])
async def test_a_path_credential_stays_one_segment(monkeypatch, account):
    if account == "..":
        with pytest.raises(GatewayFailed) as exc:
            await _relayed(monkeypatch, "https://api.example/api/{account_id}/x/", account)
        assert exc.value.kind == "injection_failed"
        return
    req = await _relayed(monkeypatch, "https://api.example/api/{account_id}/x/", account)
    assert req.url.raw_path == b"/api/a%2Fb/x/"


def test_fill_leaves_a_url_without_values_unchanged():
    url = "https://api.example/api/{account_id}/x/"
    assert pathsegment.fill(url, {}) == url


def test_catalog_call_leaves_a_credential_placeholder_for_the_relay():
    provider = OAuthProvider(
        service="pathcred", display_name="P", auth_kind="key", token_header="X-API-KEY",
        extra_credential_label="Account ID", extra_credential_location="path",
        extra_credential_param="account_id", base_url="https://api.example",
        auth_uri="", token_uri="", scopes={}, client_id_setting="", client_secret_setting="")
    ep = {"id": "pathcred.x", "path": "/api/{account_id}/items/{item_id}/", "input": {}}
    url, consumed = call_resolution._marketplace_upstream(
        ep, provider, {"account_id": "9999", "item_id": "7"})
    assert url == "https://api.example/api/{account_id}/items/7/"
    assert consumed == {"account_id", "item_id"}
