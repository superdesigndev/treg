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


class _Staged(httpcore.AsyncNetworkBackend):
    """Each address either hangs until cancelled, fails at once, or answers."""

    def __init__(self, behaviour: dict[str, str]) -> None:
        self.behaviour = behaviour
        self.started: list[str] = []
        self.closed: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        import asyncio
        self.started.append(host)
        kind = self.behaviour[host]
        if kind == "fail":
            raise httpcore.ConnectError(f"{host} refused")
        if kind == "hang":
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.Event().wait()
            except TimeoutError as exc:
                raise httpcore.ConnectTimeout(f"{host} timed out") from exc
        backend = self

        class _Stream(httpcore.AsyncMockStream):
            async def aclose(self) -> None:
                backend.closed.append(host)
        return _Stream([])

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    async def sleep(self, seconds):
        import asyncio
        await asyncio.sleep(seconds)


def _backend(staged: _Staged, *ips: str) -> pinned.PublicOnlyBackend:
    return pinned.PublicOnlyBackend(staged, _answers(list(ips)))


async def test_a_dead_first_address_does_not_cost_the_whole_timeout():
    import time
    staged = _Staged({"8.8.8.8": "hang", "1.1.1.1": "ok"})
    began = time.monotonic()
    await _backend(staged, "8.8.8.8", "1.1.1.1").connect_tcp("two.example", 443, timeout=2.0)
    assert time.monotonic() - began < 0.6, "the next address starts after the stagger, not the timeout"
    assert staged.started == ["8.8.8.8", "1.1.1.1"]


async def test_a_refused_address_hands_over_at_once():
    import time
    staged = _Staged({"8.8.8.8": "fail", "1.1.1.1": "ok"})
    began = time.monotonic()
    await _backend(staged, "8.8.8.8", "1.1.1.1").connect_tcp("two.example", 443, timeout=2.0)
    assert time.monotonic() - began < 0.1


async def test_many_dead_addresses_share_one_timeout():
    import time
    ips = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9"]
    staged = _Staged({ip: "hang" for ip in ips})
    began = time.monotonic()
    with pytest.raises(httpcore.ConnectTimeout):
        await _backend(staged, *ips).connect_tcp("dead.example", 443, timeout=0.4)
    assert time.monotonic() - began < 0.8, "N dead addresses must not cost N timeouts"


async def test_only_the_winning_connection_stays_open():
    staged = _Staged({"8.8.8.8": "ok", "1.1.1.1": "ok"})
    stream = await _backend(staged, "8.8.8.8", "1.1.1.1").connect_tcp("two.example", 443, timeout=2.0)
    assert stream is not None and staged.started == ["8.8.8.8"] and staged.closed == []
