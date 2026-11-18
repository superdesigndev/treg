"""The call-time address lookup runs off the event loop, one lookup per host at a time."""

import asyncio
import contextlib
import logging
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

    async def fake_send(self, request, **kw):
        return httpx.Response(200, json={"client_id": str(request.url), "client_name": "x",
                                         "redirect_uris": ["https://slow.example/cb"]},
                              request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    doc, gaps = await _ticking(mcp_oauth.fetch_client_id_metadata("https://slow.example/cimd.json"))
    assert doc is not None
    assert [name.startswith("treg-ssrf-dns-metadata") for _, name in calls] == [True]
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
            assert caught.value.log_kind == "ssrf_lookup_timeout"
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


async def test_a_lookup_timeout_logs_a_warning(dead_resolver, caplog):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        with caplog.at_level(logging.WARNING, logger="treg.ssrf"), pytest.raises(GatewayFailed):
            await _relay("quiet.example", client)
    assert any("ssrf_lookup_timeout" in r.message and "quiet.example" in r.message for r in caplog.records)


@pytest.fixture
def fresh_pools(monkeypatch):
    """Lookup pools sized by the test, shut down after it."""
    from treg.infra.upstream import ssrf

    configure_mappers()
    saved = dict(ssrf._executors)
    ssrf._executors.clear()
    yield relay_module.get_settings()
    for executor in ssrf._executors.values():
        executor.shutdown(wait=False, cancel_futures=True)
    ssrf._executors.clear()
    ssrf._executors.update(saved)
    ssrf._inflight.clear()


async def test_a_metadata_flood_never_delays_a_provider_lookup(fresh_pools, monkeypatch):
    """Requests before sign-in name a new never-answering host each; the call road's lookups run on
    their own threads, during the flood and after it."""
    from treg.domain.identity import mcp_oauth
    from treg.infra.upstream import ssrf

    s = fresh_pools
    monkeypatch.setattr(s, "proxy_ssrf_check", True)
    monkeypatch.setattr(s, "ssrf_lookup_timeout_s", 0.1)
    monkeypatch.setattr(s, "ssrf_lookup_threads", 2)
    monkeypatch.setattr(s, "ssrf_metadata_lookup_threads", 2)
    release = threading.Event()

    def getaddrinfo(host, port, *a, **k):
        if host.endswith(".dead.example"):
            release.wait(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    open_requests = asyncio.Semaphore(4)

    async def attacker(i):
        async with open_requests:
            assert await mcp_oauth.fetch_client_id_metadata(f"https://a{i}.dead.example/c.json") is None

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
            flood = asyncio.gather(*(attacker(i) for i in range(40)))
            waits = []
            while not flood.done():
                started = time.monotonic()
                await _relay("api.provider.example", client)
                waits.append(time.monotonic() - started)
                pending = sum(1 for e in ssrf._inflight.values() if e.pool == ssrf.METADATA)
                assert pending <= 2 * ssrf._METADATA_PENDING_PER_THREAD
                await asyncio.sleep(0.02)
            await flood
            started = time.monotonic()
            await _relay("api.provider.example", client)
            waits.append(time.monotonic() - started)
    finally:
        release.set()
    assert waits and max(waits) < 0.05


async def test_a_queued_lookup_is_cancelled_when_its_last_caller_gives_up(fresh_pools, monkeypatch):
    from treg.infra.upstream import ssrf

    s = fresh_pools
    monkeypatch.setattr(s, "ssrf_lookup_timeout_s", 0.1)
    monkeypatch.setattr(s, "ssrf_lookup_threads", 1)
    release, asked = threading.Event(), []

    def check(host):
        asked.append(host)
        release.wait(10)
        return True

    try:
        running = asyncio.ensure_future(ssrf.lookup_host("first.example", check))
        await asyncio.sleep(0.02)
        assert await ssrf.lookup_host("queued.example", check) == ssrf.TIMED_OUT
        assert (ssrf.CALL, "queued.example") not in ssrf._inflight
        assert await running == ssrf.TIMED_OUT
        assert (ssrf.CALL, "first.example") in ssrf._inflight, "a running lookup is joined, not repeated"
    finally:
        release.set()
    await asyncio.sleep(0.1)
    assert asked == ["first.example"]


async def test_continuous_traffic_exits_after_recovery(monkeypatch):
    configure_mappers()
    s = relay_module.get_settings()
    monkeypatch.setattr(s, "proxy_ssrf_check", True)
    monkeypatch.setattr(s, "ssrf_lookup_timeout_s", 0.1)
    gate = threading.Event()

    def getaddrinfo(host, port, *a, **k):
        if not gate.is_set():
            gate.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    first_ok_after = None
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
            start = time.monotonic()
            recovered_at = None
            while time.monotonic() - start < 4 and first_ok_after is None:
                if recovered_at is None and time.monotonic() - start > 1.5:
                    gate.set()
                    recovered_at = time.monotonic()
                res = await asyncio.gather(*(_relay(f"h{i}.example", client) for i in range(20)),
                                           return_exceptions=True)
                if recovered_at and all(not isinstance(r, Exception) for r in res):
                    first_ok_after = time.monotonic() - recovered_at
                await asyncio.sleep(0.01)
    finally:
        gate.set()
    assert first_ok_after is not None and first_ok_after < 1.0


@contextlib.asynccontextmanager
async def _metadata_server(monkeypatch, write_body):
    """A real local HTTP server answering every metadata fetch 200 with what `write_body` writes;
    the https URL the fetch asks for is sent to it in plain HTTP."""
    from treg import health

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")
            await write_body(writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_send = httpx.AsyncClient.send

    async def send(self, request, **kw):
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
        return await real_send(self, request, **kw)

    monkeypatch.setattr(health, "safe_webhook_url", lambda u: True)
    monkeypatch.setattr(health, "host_is_public", lambda h: True)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    try:
        yield
    finally:
        server.close()


def _chunk(data: bytes) -> bytes:
    return b"%x\r\n%s\r\n" % (len(data), data)


async def test_a_metadata_server_sending_one_byte_at_a_time_is_cut_at_the_deadline(monkeypatch):
    from treg.domain.identity import mcp_oauth

    async def drip(writer):
        for _ in range(100):
            writer.write(_chunk(b" "))
            await writer.drain()
            await asyncio.sleep(0.05)

    monkeypatch.setattr(mcp_oauth, "_CIMD_TIMEOUT_S", 0.5)
    async with _metadata_server(monkeypatch, drip):
        started = time.monotonic()
        assert await mcp_oauth.fetch_client_id_metadata("https://drip.example/c.json") is None
        assert time.monotonic() - started < 1.0


async def test_a_metadata_body_is_read_no_further_than_the_size_cap(monkeypatch):
    from treg.domain.identity import mcp_oauth

    piece, pieces, sent = 64 * 1024, 64, []

    async def big(writer):
        for _ in range(pieces):
            writer.write(_chunk(b" " * piece))
            await writer.drain()
            sent.append(piece)
        writer.write(b"0\r\n\r\n")
        await writer.drain()

    async with _metadata_server(monkeypatch, big):
        assert await mcp_oauth.fetch_client_id_metadata("https://big.example/c.json") is None
        await asyncio.sleep(0.2)
    assert sum(sent) < piece * pieces, "the fetch stopped reading at the cap"


async def test_no_database_transaction_is_open_while_the_metadata_document_is_fetched(clients, monkeypatch):
    from treg.application import auth
    from treg.domain.identity import mcp_oauth
    from treg.infra.db import session_maker

    seen = []
    async with session_maker() as db:
        async def fetch(url):
            seen.append(db.in_transaction())
            return None

        monkeypatch.setattr(mcp_oauth, "fetch_client_id_metadata", fetch)
        assert await auth._resolve_oauth_client("https://new-client.example/c.json", db) is None
    assert seen == [False]
