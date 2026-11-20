"""The relay carries a caller's declared Content-Length upstream instead of re-framing the body chunked.

Meta's Graph API edge does not read a `Transfer-Encoding: chunked` request body: every relayed
POST arrived bodyless and an ad creative failed "Ad incomplete" (live, 2026-09-19). The bytes are
the caller's, unaltered, so the caller's own length is exact.
"""

from __future__ import annotations

import json

import httpx
import pytest

from treg.application.call.types import GatewayFailed, UpstreamRequest
from treg.infra.upstream import relay as relay_module
from treg.models import Tool


def _tool() -> Tool:
    return Tool(org_id=1, name="echo", owner="t", base_url="https://echo.example",
                host="echo.example", bindings=[])


async def _relay(monkeypatch, raw_headers: tuple[tuple[bytes, bytes], ...]) -> httpx.Headers:
    seen: dict[str, httpx.Headers] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = req.headers
        try:
            seen["body"] = req.content
        except httpx.RequestNotRead:
            seen["body"] = b"".join([chunk async for chunk in req.stream])
        return httpx.Response(200, content=b"ok")

    async def body():
        yield b"abc"

    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", False, raising=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        req = UpstreamRequest(method="POST", raw_headers=raw_headers, query_items=(),
                              body_stream=body, has_body=True)
        resp = await relay_module.relay(req, "https://echo.example/p", _tool(), {}, client)
        assert resp.status == 200
        await resp.close()
    assert seen["body"] == b"abc"
    return seen["headers"]


@pytest.mark.asyncio
async def test_declared_content_length_is_carried_not_chunked(monkeypatch) -> None:
    headers = await _relay(monkeypatch, ((b"content-type", b"application/json"),
                                         (b"content-length", b"3")))
    assert headers.get("content-length") == "3"
    assert "transfer-encoding" not in headers


@pytest.mark.asyncio
async def test_chunked_caller_stays_chunked(monkeypatch) -> None:
    headers = await _relay(monkeypatch, ((b"content-type", b"application/json"),
                                         (b"transfer-encoding", b"chunked")))
    assert headers.get("transfer-encoding") == "chunked"
    assert "content-length" not in headers


@pytest.mark.asyncio
async def test_json_bindings_overwrite_credentials_and_recompute_length(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        body = await req.aread()
        seen.update(raw=body, body=json.loads(body), length=req.headers.get("content-length"))
        return httpx.Response(200, content=b"ok")

    raw = '{"company_domain":"caf\u00e9.example","api_key":"caller-value"}'.encode()

    async def stream():
        yield raw

    async def read():
        return raw

    settings = relay_module.get_settings()
    monkeypatch.setattr(settings, "proxy_ssrf_check", False, raising=False)
    monkeypatch.setattr(settings, "platform_key_adyntel", "server-key", raising=False)
    monkeypatch.setattr(settings, "platform_email_adyntel", "owner@example.com", raising=False)
    tool = Tool(org_id=1, name="adyntel", owner="t", base_url="https://api.adyntel.com",
                host="api.adyntel.com", bindings=[
                    {"platform_setting": "platform_key_adyntel", "injector": "env",
                     "location": "json", "name": "api_key", "format": "{secret}"},
                    {"platform_setting": "platform_email_adyntel", "injector": "env",
                     "location": "json", "name": "email", "format": "{secret}"},
                ])
    req = UpstreamRequest(
        method="POST",
        raw_headers=((b"content-type", b"application/json"),
                     (b"content-length", str(len(raw)).encode())),
        query_items=(), body_stream=stream, has_body=True, body_read=read,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await relay_module.relay(req, "https://api.adyntel.com/facebook", tool, {}, client)
        await resp.close()

    assert seen["body"] == {
        "company_domain": "caf\u00e9.example", "api_key": "server-key", "email": "owner@example.com",
    }
    assert b"caf\xc3\xa9.example" in seen["raw"]
    assert b"\\u00e9" not in seen["raw"]
    assert seen["length"] == str(len(seen["raw"]))


@pytest.mark.asyncio
async def test_json_bindings_reject_duplicate_object_keys(monkeypatch) -> None:
    raw = b'{"company_domain":"one.example","company_domain":"two.example"}'

    async def stream():
        yield raw

    async def read():
        return raw

    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", False, raising=False)
    monkeypatch.setattr(
        relay_module.get_settings(), "platform_key_adyntel", "server-key", raising=False,
    )
    tool = Tool(
        org_id=1, name="adyntel", owner="t", base_url="https://api.adyntel.com",
        host="api.adyntel.com", bindings=[{
            "platform_setting": "platform_key_adyntel", "injector": "env",
            "location": "json", "name": "api_key", "format": "{secret}",
        }],
    )
    req = UpstreamRequest(
        method="POST", raw_headers=((b"content-type", b"application/json"),),
        query_items=(), body_stream=stream, has_body=True, body_read=read,
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(GatewayFailed) as exc:
            await relay_module.relay(req, "https://api.adyntel.com/facebook", tool, {}, client)
    assert exc.value.status_code == 502
    assert "unique object keys" in str(exc.value.detail)
