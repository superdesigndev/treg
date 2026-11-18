"""The call-time address lookup runs off the event loop, one lookup per host at a time."""

import asyncio
import socket
import threading
import time

import httpx
import pytest

from treg.application.call.types import GatewayFailed, UpstreamRequest
from treg.infra.upstream import relay as relay_module
from treg.models import Tool

LOOKUP_S = 0.3


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
    assert max(gaps) < LOOKUP_S / 2
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
