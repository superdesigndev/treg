"""The call-time address lookup runs off the event loop, one lookup per host at a time."""

import asyncio
import socket
import threading
import time

import httpx
import pytest
from sqlalchemy.orm import configure_mappers

from treg.application.call.types import GatewayFailed, UpstreamRequest
from treg.infra.upstream import relay as relay_module
from treg.models import Tool

LOOKUP_S = 0.3
BLOCKED = LOOKUP_S * 0.8  # a lookup on the loop stalls it for at least LOOKUP_S


def _request():
    async def stream():
        yield b""

    async def read():
        return b""

    return UpstreamRequest(method="GET", raw_headers=(), query_items=(), body_stream=stream,
                           has_body=False, body_read=read)


def _tool(host):
    return Tool(org_id=1, name="example", owner="test", base_url=f"https://{host}", host=host, bindings=[])


@pytest.fixture
def slow_lookup(monkeypatch):
    configure_mappers()  # the first Tool() otherwise configures every mapper inside the timed window
    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", True)
    calls = []
    answers = {}

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append((host, threading.current_thread().name))
        time.sleep(LOOKUP_S)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers.get(host, "8.8.8.8"), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls, answers


async def _relay(host, client):
    return await relay_module.relay(_request(), f"https://{host}/x", _tool(host), {}, client)


async def test_slow_lookup_does_not_block_the_loop(slow_lookup):
    calls, _ = slow_lookup
    gaps = []

    async def ticker():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        tick = asyncio.create_task(ticker())
        started = time.monotonic()
        await asyncio.gather(*(_relay(f"h{i}.example", client) for i in range(4)))
        elapsed = time.monotonic() - started
        tick.cancel()
    assert len(calls) == 4
    assert all(name.startswith("treg-ssrf-dns") for _, name in calls)
    assert max(gaps) < BLOCKED
    assert elapsed < LOOKUP_S * 3


async def test_one_lookup_per_host_in_flight(slow_lookup):
    calls, _ = slow_lookup
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        await asyncio.gather(*(_relay("same.example", client) for _ in range(10)))
        assert len(calls) == 1
        await _relay("same.example", client)
    assert len(calls) == 2


async def test_verdicts_are_unchanged(slow_lookup):
    _, answers = slow_lookup
    answers["meta.example"] = "169.254.169.254"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        with pytest.raises(GatewayFailed) as caught:
            await _relay("meta.example", client)
        assert caught.value.kind == "ssrf_refused"
        assert await _relay("ok.example", client) is not None


async def _ticking(coro):
    gaps = []

    async def ticker():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        tick.cancel()
    return result, gaps


async def test_client_metadata_lookup_does_not_block_the_loop(slow_lookup, monkeypatch):
    from treg.domain.identity import mcp_oauth

    calls, _ = slow_lookup

    async def fake_get(self, url, **kw):
        return httpx.Response(200, json={"client_id": url, "client_name": "x",
                                         "redirect_uris": ["https://slow.example/cb"]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    doc, gaps = await _ticking(mcp_oauth.fetch_client_id_metadata("https://slow.example/cimd.json"))
    assert doc is not None
    assert [name.startswith("treg-ssrf-dns") for _, name in calls] == [True]
    assert max(gaps) < BLOCKED


@pytest.fixture
def dead_resolver(monkeypatch):
    """A name server that never answers until released; every lookup waits on the same gate."""
    monkeypatch.setattr(relay_module.get_settings(), "proxy_ssrf_check", True)
    monkeypatch.setattr(relay_module.get_settings(), "ssrf_lookup_timeout_s", 0.1)
    gate = threading.Event()

    def getaddrinfo(host, port, *args, **kwargs):
        gate.wait(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    try:
        yield gate
    finally:
        gate.set()


async def test_a_dead_resolver_refuses_within_the_limit_under_traffic_and_recovers(dead_resolver):
    from treg.infra.upstream import ssrf

    gate = dead_resolver
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        async def one(host):
            started = time.monotonic()
            with pytest.raises(GatewayFailed) as caught:
                await _relay(host, client)
            assert caught.value.kind == "ssrf_refused"
            return time.monotonic() - started

        # More distinct hosts than lookup threads, in waves: later callers queue behind busy
        # threads or join a pending lookup, and each still gets its answer at the limit.
        waits = []
        for _ in range(3):
            waits += await asyncio.gather(*(one(f"h{i}.example") for i in range(20)))
        assert max(waits) < 0.5

        gate.set()
        for _ in range(100):
            if not ssrf._inflight:
                break
            await asyncio.sleep(0.02)
        assert not ssrf._inflight
        assert await _relay("h0.example", client) is not None
        assert await _relay("new.example", client) is not None


async def test_client_metadata_is_refused_when_the_lookup_outlasts_the_limit(dead_resolver):
    from treg.domain.identity import mcp_oauth

    started = time.monotonic()
    assert await mcp_oauth.fetch_client_id_metadata("https://dead-ns.example/cimd.json") is None
    assert time.monotonic() - started < 0.5
