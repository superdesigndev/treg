"""The call-time address check refuses what it cannot confirm public, without blocking the event loop."""

import asyncio
import socket
import threading
import time

import httpx
import pytest

from treg.config import get_settings
from treg.infra.upstream import ssrf


@pytest.fixture
def resolver(monkeypatch):
    answers: dict[str, list[str] | None] = {}
    gate = threading.Event()
    slow: set[str] = set()

    def getaddrinfo(host, port, *args, **kwargs):
        if host in slow:
            gate.wait(5)
        found = answers.get(host)
        if found is None:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in found]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield answers, slow, gate
    gate.set()


async def test_unresolvable_host_is_refused(resolver):
    assert await ssrf.host_is_public_async("nowhere.example") is False


async def test_private_address_is_refused(resolver):
    answers, _, _ = resolver
    answers["inside.example"] = ["10.0.0.5"]
    answers["mixed.example"] = ["8.8.8.8", "169.254.169.254"]
    answers["outside.example"] = ["8.8.8.8"]
    assert await ssrf.host_is_public_async("inside.example") is False
    assert await ssrf.host_is_public_async("mixed.example") is False
    assert await ssrf.host_is_public_async("outside.example") is True


async def test_a_lookup_past_the_timeout_is_refused_without_blocking_the_loop(resolver, monkeypatch):
    answers, slow, gate = resolver
    answers["hung.example"] = ["8.8.8.8"]
    slow.add("hung.example")
    monkeypatch.setattr(get_settings(), "ssrf_lookup_timeout_s", 0.2)
    started = time.monotonic()
    verdict, gaps = await _gaps_while(ssrf.host_is_public_async("hung.example"))
    elapsed = time.monotonic() - started
    assert verdict is False
    assert 0.2 <= elapsed < 1
    assert gaps and max(gaps) < 0.1


async def test_a_timeout_is_not_remembered(resolver, monkeypatch):
    answers, slow, gate = resolver
    answers["flaky.example"] = ["8.8.8.8"]
    slow.add("flaky.example")
    monkeypatch.setattr(get_settings(), "ssrf_lookup_timeout_s", 0.1)
    assert await ssrf.host_is_public_async("flaky.example") is False
    gate.set()
    for _ in range(200):
        if "flaky.example" not in ssrf._inflight:
            break
        await asyncio.sleep(0.01)
    assert "flaky.example" not in ssrf._inflight
    assert await ssrf.host_is_public_async("flaky.example") is True


async def _gaps_while(coro):
    gaps = []

    async def ticker():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    result = await coro
    tick.cancel()
    return result, gaps


async def test_client_metadata_fetch_resolves_off_the_loop(resolver, monkeypatch):
    from treg.domain.identity import mcp_oauth

    answers, slow, _ = resolver
    answers["cimd.example"] = ["8.8.8.8"]
    slow.add("cimd.example")
    monkeypatch.setattr(get_settings(), "ssrf_lookup_timeout_s", 0.3)
    calls = []

    async def fake_get(self, url, **kw):
        calls.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    doc, gaps = await _gaps_while(mcp_oauth.fetch_client_id_metadata("https://cimd.example/client.json"))
    assert doc is None
    assert calls == []  # refused on the lookup timeout, never fetched
    assert gaps and max(gaps) < 0.15


async def test_client_metadata_fetch_refuses_a_private_host(resolver, monkeypatch):
    from treg.domain.identity import mcp_oauth

    answers, _, _ = resolver
    answers["rebound.example"] = ["127.0.0.1"]
    calls = []

    async def fake_get(self, url, **kw):
        calls.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await mcp_oauth.fetch_client_id_metadata("https://rebound.example/client.json") is None
    assert calls == []


@pytest.fixture
def one_lookup_thread(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="treg-ssrf-dns")
    monkeypatch.setattr(ssrf, "_executor", pool)
    yield
    pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def counted_resolver(resolver, monkeypatch):
    answers, slow, gate = resolver
    resolved = []
    inner = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        resolved.append(host)
        return inner(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return answers, slow, gate, resolved


async def _drain(host):
    for _ in range(300):
        if host not in ssrf._inflight:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{host} still in flight")


async def test_a_queued_lookup_nobody_waits_for_is_dropped(counted_resolver, one_lookup_thread, monkeypatch):
    answers, slow, gate, resolved = counted_resolver
    answers["busy.example"] = answers["queued.example"] = ["8.8.8.8"]
    slow.add("busy.example")
    monkeypatch.setattr(get_settings(), "ssrf_lookup_timeout_s", 0.1)
    busy = asyncio.create_task(ssrf.host_is_public_async("busy.example"))
    await asyncio.sleep(0.02)
    assert await ssrf.host_is_public_async("queued.example") is False  # timed out in the queue
    assert "queued.example" not in ssrf._inflight
    gate.set()
    await busy
    await _drain("busy.example")
    await asyncio.sleep(0.05)
    assert resolved == ["busy.example"]


async def test_one_waiter_timing_out_keeps_the_lookup_another_still_needs(
        counted_resolver, one_lookup_thread, monkeypatch):
    answers, slow, gate, resolved = counted_resolver
    answers["busy.example"] = answers["shared.example"] = ["8.8.8.8"]
    slow.add("busy.example")
    settings = get_settings()
    monkeypatch.setattr(settings, "ssrf_lookup_timeout_s", 2.0)
    busy = asyncio.create_task(ssrf.host_is_public_async("busy.example"))
    await asyncio.sleep(0.02)
    monkeypatch.setattr(settings, "ssrf_lookup_timeout_s", 0.1)
    impatient = asyncio.create_task(ssrf.host_is_public_async("shared.example"))
    await asyncio.sleep(0)
    monkeypatch.setattr(settings, "ssrf_lookup_timeout_s", 2.0)
    patient = asyncio.create_task(ssrf.host_is_public_async("shared.example"))
    assert await impatient is False
    gate.set()
    assert await patient is True
    await busy
    assert resolved == ["busy.example", "shared.example"]
