"""The upstream connection opens to the address the SSRF check approved, never a second lookup's."""

from __future__ import annotations

import socket

import httpcore
import httpx
import pytest

from treg.application.call.types import UpstreamRequest
from treg.config import Settings, get_settings
from treg.infra.upstream import pinned
from treg.infra.upstream.relay import GatewayFailed, relay
from treg.models import Tool

_OK = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]


class _Recording(httpcore.AsyncMockBackend):
    def __init__(self) -> None:
        super().__init__(list(_OK))
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        return await super().connect_tcp(host, port, timeout, local_address, socket_options)


def _answers(*sequence: list[str]):
    """A resolver whose successive lookups answer each list in turn: DNS rebinding."""
    calls = iter(sequence)

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in next(calls)]
    return getaddrinfo


async def test_the_connection_goes_to_the_checked_address():
    backend = _Recording()
    transport = pinned.public_only_transport(backend=backend, getaddrinfo=_answers(["93.184.215.14"]))
    async with httpx.AsyncClient(transport=transport) as client:
        r = await client.get("http://rebind.example/x")
    assert r.status_code == 200 and r.request.headers["host"] == "rebind.example"
    assert backend.connected == [("93.184.215.14", 80)]


@pytest.mark.parametrize("answer", [["127.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "10.0.0.5"], []])
async def test_a_non_public_answer_never_connects(answer):
    backend = _Recording()
    transport = pinned.public_only_transport(backend=backend, getaddrinfo=_answers(answer))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError) as caught:
            await client.get("http://rebind.example/x")
    assert pinned.is_refusal(caught.value)
    assert backend.connected == []


async def test_rebinding_after_the_check_is_refused_by_the_relay(monkeypatch):
    """The pre-send check sees a public address; the connection's own lookup gets loopback."""
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", True)
    monkeypatch.setattr(socket, "getaddrinfo", _answers(["8.8.8.8"]))
    backend = _Recording()
    transport = pinned.public_only_transport(backend=backend, getaddrinfo=_answers(["127.0.0.1"]))
    tool = Tool(org_id=1, name="rebind", owner="o@x.dev", base_url="http://rebind.example",
                host="rebind.example", bindings=[])

    async def empty():
        if False:
            yield b""

    request = UpstreamRequest(method="GET", raw_headers=(), query_items=(), body_stream=empty, has_body=False)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(GatewayFailed) as caught:
            await relay(request, "http://rebind.example/x", tool, {}, client)
    assert caught.value.kind == "ssrf_refused"
    assert backend.connected == []


def test_upstream_clients_connect_through_the_guard(monkeypatch):
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", True)
    client = pinned.upstream_client(timeout=5)
    assert isinstance(client._transport._pool._network_backend, pinned.PublicOnlyBackend)
    assert client._mounts == {}, "an environment proxy would resolve the name outside the guard"


@pytest.mark.parametrize("database_url,check,missing", [
    ("postgresql+asyncpg://u@db/treg", False, True),
    ("postgresql+asyncpg://u@db/treg", True, False),
    ("sqlite+aiosqlite:///./local.db", False, False),
])
def test_a_real_deploy_cannot_serve_without_the_guard(database_url, check, missing):
    s = Settings(database_url=database_url, proxy_ssrf_check=check)
    assert s.ssrf_check_missing is missing
