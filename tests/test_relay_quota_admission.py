"""Quota admission follows local relay validation and immediately precedes transport."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from treg.application.call.types import AuthorizationFailed, GatewayFailed, UpstreamRequest
from treg.infra.upstream import health
from treg.infra.upstream import relay as relay_module
from treg.models import Tool


def _request(raw=b'{"query":"synthetic-private-input"}', *, has_body=True):
    async def stream():
        yield raw

    async def read():
        return raw

    return UpstreamRequest(method="POST", raw_headers=((b"content-type", b"application/json"),),
                           query_items=(), body_stream=stream, has_body=has_body, body_read=read)


def _tool(bindings=None):
    return Tool(org_id=1, name="example", owner="test", base_url="https://echo.example",
                host="echo.example", bindings=bindings or [])


@pytest.mark.parametrize("failure", [
    "invalid_json", "duplicate_json", "non_object_json", "missing_body",
    "missing_credential", "invalid_binding", "ssrf",
])
async def test_local_relay_rejection_never_calls_quota_admission(monkeypatch, failure):
    settings = relay_module.get_settings()
    monkeypatch.setattr(settings, "proxy_ssrf_check", failure == "ssrf")
    monkeypatch.setattr(settings, "platform_key_adyntel", "synthetic-key")
    monkeypatch.setattr(health, "host_is_public", lambda host: False)
    binding = {"platform_setting": "platform_key_adyntel", "injector": "env",
               "location": "json", "name": "api_key", "format": "{secret}"}
    raw = b'{"query":"synthetic-private-input"}'
    if failure == "invalid_json":
        raw = b'synthetic-private-input'
    elif failure == "duplicate_json":
        raw = b'{"query":"synthetic-private-input","query":"duplicate"}'
    elif failure == "non_object_json":
        raw = b'["synthetic-private-input"]'
    elif failure == "missing_credential":
        monkeypatch.setattr(settings, "platform_key_adyntel", "")
    elif failure == "invalid_binding":
        binding["injector"] = "unknown"
    tool = _tool([] if failure == "ssrf" else [binding])
    request = _request(raw, has_body=failure != "missing_body")
    admission = AsyncMock()
    outbound = AsyncMock(side_effect=AssertionError("local refusal cannot send upstream"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
        with pytest.raises(GatewayFailed) as caught:
            await relay_module.relay(request, "https://echo.example/read", tool, {}, client,
                                     before_outbound=admission)
    assert caught.value.kind == ("ssrf_refused" if failure == "ssrf" else "injection_failed")
    assert "synthetic-private-input" not in str(caught.value.detail)
    assert "synthetic-key" not in str(caught.value.detail)
    admission.assert_not_called()
    outbound.assert_not_called()


async def test_quota_refusal_after_validation_stops_real_transport(monkeypatch):
    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", False)
    failure = AuthorizationFailed("org_endpoint_quota_exceeded", status_code=429,
                                  detail={"error": "org_endpoint_quota_exceeded"})
    admission = AsyncMock(side_effect=failure)
    outbound = AsyncMock(side_effect=AssertionError("quota refusal cannot send upstream"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
        with pytest.raises(AuthorizationFailed) as caught:
            await relay_module.relay(_request(), "https://echo.example/read", _tool(), {}, client,
                                     before_outbound=admission)
    assert caught.value is failure
    admission.assert_awaited_once_with()
    outbound.assert_not_called()


async def test_cancelled_admission_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", False)
    admission = AsyncMock(side_effect=asyncio.CancelledError)
    outbound = AsyncMock(side_effect=AssertionError("cancelled admission cannot send upstream"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
        with pytest.raises(asyncio.CancelledError):
            await relay_module.relay(_request(), "https://echo.example/read", _tool(), {}, client,
                                     before_outbound=admission)
    admission.assert_awaited_once_with()
    outbound.assert_not_called()


async def test_admission_runs_once_immediately_before_transport(monkeypatch):
    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", True)
    events = []

    def public_host(host):
        events.append("ssrf")
        return True

    async def admit():
        events.append("admit")

    async def outbound(request):
        events.append("send")
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(health, "host_is_public", public_host)
    async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
        response = await relay_module.relay(
            _request(), "https://echo.example/read", _tool(), {}, client, before_outbound=admit)
        assert response.status == 200
        await response.close()
    assert events == ["ssrf", "admit", "send"]
