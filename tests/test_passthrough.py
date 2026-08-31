"""URL-passthrough + faithful relay.

The agent passes the REAL upstream URL (it already knows the API); treg resolves the tool by
host + longest base_url prefix, injects, and relays everything verbatim (methods, all query
params incl. duplicates, arbitrary headers, cookies, body) — touching only transport headers,
our control token, and the injected credential.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import AsyncClient

from treg.api import app


class _CloseTrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: int = 1000) -> None:
        self.chunks = chunks
        self.close_calls = 0
        self.chunks_yielded = 0
        self.exhausted = False

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.chunks_yielded += 1
            yield b"chunk"
            await asyncio.sleep(0)
        self.exhausted = True

    async def aclose(self) -> None:
        self.close_calls += 1


class _CloseTrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: _CloseTrackingStream) -> None:
        self.stream = stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              stream=self.stream, request=request)


async def _register(c: AsyncClient, name: str, base_url: str, value: str = "SEK") -> None:
    sid = (await c.post("/secrets", json={"name": f"{name}-k", "value": value})).json()["id"]
    r = await c.post("/tools", json={"name": name, "base_url": base_url, "secret_id": sid})
    assert r.status_code == 200, r.text


async def test_passthrough_resolves_by_url_and_injects(clients: AsyncClient):
    await _register(clients, "intercom", "https://api.intercom.io")
    r = await clients.get("/call/https://api.intercom.io/echo?per_page=5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["auth"] == "Bearer SEK"          # credential injected by treg
    assert d["query"]["per_page"] == "5"      # caller's real query preserved


async def test_completed_relay_closes_the_upstream_response(clients: AsyncClient):
    """A downstream disconnect must run the close task before httpx exhausts its own iterator."""
    stream = _CloseTrackingStream()
    tracked = AsyncClient(transport=_CloseTrackingTransport(stream), base_url="http://tracked")
    original = app.state.http
    app.state.http = tracked
    try:
        await _register(clients, "tracked", "http://tracked")
        response_started = asyncio.Event()
        five_chunks_sent = asyncio.Event()
        body_chunks = 0
        request_delivered = False

        async def receive():
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await response_started.wait()
            await five_chunks_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal body_chunks
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                response_started.set()
            elif message["type"] == "http.response.body" and message.get("body"):
                body_chunks += 1
                if body_chunks == 5:
                    five_chunks_sent.set()

        token = clients.headers["X-Treg-Token"].encode("latin-1")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/call/tracked/resource",
            "raw_path": b"/call/tracked/resource",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"registry"), (b"x-treg-token", token)],
            "client": ("127.0.0.1", 50000),
            "server": ("registry", 80),
        }
        async with asyncio.timeout(2):
            await app(scope, receive, send)

        assert body_chunks >= 5
        assert stream.exhausted is False
        assert stream.chunks_yielded < 1000
        assert stream.close_calls == 1
    finally:
        app.state.http = original
        await tracked.aclose()


async def test_fully_consumed_relay_closes_the_upstream_response_once(clients: AsyncClient):
    stream = _CloseTrackingStream(chunks=3)
    tracked = AsyncClient(transport=_CloseTrackingTransport(stream), base_url="http://tracked-full")
    original = app.state.http
    app.state.http = tracked
    try:
        await _register(clients, "tracked-full", "http://tracked-full")
        response = await clients.get("/call/tracked-full/resource")

        assert response.status_code == 200
        assert response.content == b"chunk" * 3
        assert stream.exhausted is True
        assert stream.close_calls == 1
    finally:
        app.state.http = original
        await tracked.aclose()


async def test_interrupted_relay_closes_the_upstream_response_once(clients: AsyncClient):
    class InterruptedStream(_CloseTrackingStream):
        async def __aiter__(self):
            self.chunks_yielded += 1
            yield b"partial"
            raise httpx.ReadError("provider stream interrupted")

    stream = InterruptedStream()
    tracked = AsyncClient(transport=_CloseTrackingTransport(stream), base_url="http://tracked-error")
    original = app.state.http
    app.state.http = tracked
    try:
        await _register(clients, "tracked-error", "http://tracked-error")
        with pytest.raises(httpx.ReadError, match="provider stream interrupted"):
            await clients.get("/call/tracked-error/resource")

        assert stream.close_calls == 1
    finally:
        app.state.http = original
        await tracked.aclose()


async def test_duplicate_query_params_preserved(clients: AsyncClient):
    await _register(clients, "ex", "https://api.ex.com")
    r = await clients.get("/call/https://api.ex.com/echo?tag=a&tag=b&tag=c")
    qm = [tuple(p) for p in r.json()["query_multi"]]
    assert qm.count(("tag", "a")) == 1 and ("tag", "b") in qm and ("tag", "c") in qm  # all three kept


async def test_caller_headers_and_cookies_passthrough_and_token_stripped(clients: AsyncClient):
    await _register(clients, "hx", "https://api.hx.com")
    r = await clients.get(
        "/call/https://api.hx.com/echo",
        headers={"X-Custom": "v1", "Cookie": "a=1; b=2"},
    )
    h = r.json()["headers"]
    assert h["x-custom"] == "v1"               # arbitrary caller header relayed
    assert h["cookie"] == "a=1; b=2"           # cookies relayed verbatim
    assert "x-treg-token" not in h             # our control header never leaks upstream


async def test_control_infra_headers_and_treg_cookie_stripped(clients: AsyncClient):
    await _register(clients, "sec", "https://api.sec.com")
    r = await clients.get(
        "/call/https://api.sec.com/echo",
        headers={
            "X-Treg-Org": "superdesign", "ngrok-skip-browser-warning": "1",
            "X-Forwarded-For": "1.2.3.4", "X-Forwarded-Proto": "https", "Via": "1.1 edge",
            "X-Keep": "yes", "Cookie": "treg_session=SECRET; keep=1; treg_oauth_state=xyz",
        },
    )
    h = r.json()["headers"]
    for leak in ("x-treg-org", "ngrok-skip-browser-warning", "x-forwarded-for", "x-forwarded-proto", "via"):
        assert leak not in h, f"{leak} leaked upstream"
    assert h["x-keep"] == "yes"        # unrelated caller header preserved
    assert h["cookie"] == "keep=1"     # treg's own cookies scrubbed, other cookies kept


async def test_longest_prefix_wins(clients: AsyncClient):
    await _register(clients, "broad", "https://api.g2.com", value="BROAD")
    await _register(clients, "narrow", "https://api.g2.com/v2", value="NARROW")
    r = await clients.get("/call/https://api.g2.com/v2/echo")
    assert r.json()["auth"] == "Bearer NARROW"  # the more specific tool is chosen


async def test_ambiguous_host_409(clients: AsyncClient):
    await _register(clients, "g1", "https://api.same.com")
    await _register(clients, "g2", "https://api.same.com")  # identical base -> tie
    r = await clients.get("/call/https://api.same.com/echo")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "'g1'" in detail and "'g2'" in detail
    assert "/call/<name>/<path>" in detail


async def test_unknown_upstream_404(clients: AsyncClient):
    r = await clients.get("/call/https://nope.example.com/echo")
    assert r.status_code == 404


async def test_named_form_still_works(clients: AsyncClient):
    await _register(clients, "echo", "https://api.named.com")
    r = await clients.get("/call/echo/echo")  # <tool>/<path>
    assert r.status_code == 200
    assert r.json()["auth"] == "Bearer SEK"


async def test_orgs_reports_tool_count(clients: AsyncClient):
    """/orgs carries tool_count so the dashboard can land on the org that actually has tools
    (not a first-run default that may be an empty team)."""
    orgs = (await clients.get("/orgs")).json()
    assert orgs and all("tool_count" in o for o in orgs)
    assert sum(o["tool_count"] for o in orgs) == 0          # fresh account, no tools yet
    await _register(clients, "stripe", "https://api.stripe.com/v1")
    orgs = (await clients.get("/orgs")).json()
    assert sum(o["tool_count"] for o in orgs) == 1          # the count reflects the registered tool


async def test_encoded_slash_preserved_named_form(clients: AsyncClient):
    """An encoded slash in the path must reach the upstream still encoded (`%2f`, not `/`) —
    npm's scoped publish route (`PUT /@scope%2fname`) 404s if the proxy decodes it."""
    await _register(clients, "npmreg", "https://registry.npm.test")
    r = await clients.put("/call/npmreg/@superdesign%2ftreg", content=b"{}")
    assert r.status_code == 200, r.text
    assert r.json()["raw_path"].endswith("/@superdesign%2ftreg")


async def test_encoded_slash_preserved_passthrough_form(clients: AsyncClient):
    await _register(clients, "npmreg2", "https://registry.npm2.test")
    r = await clients.put("/call/https://registry.npm2.test/@superdesign%2ftreg", content=b"{}")
    assert r.status_code == 200, r.text
    assert r.json()["raw_path"].endswith("/@superdesign%2ftreg")


async def test_treg_marks_its_own_call_errors(clients: AsyncClient):
    """A caller cannot otherwise tell treg's 404 ("no tool registered for that host") from the
    vendor's own 404 — both are a status and some JSON. `X-Treg-Error` is the distinction the local
    proxy needs to explain a failure without ever rewriting a real vendor response. It is only ever
    ADDED: the status and body are unchanged."""
    r = await clients.get("/call/https://api.nobody-registered.com/thing")
    assert r.status_code == 404
    assert r.headers.get("x-treg-error") == "1"
    assert r.json()["detail"]                       # the original body is untouched

    await _register(clients, "marked", "https://api.marked.com")
    ok = await clients.get("/call/https://api.marked.com/echo")
    assert ok.status_code == 200 and "x-treg-error" not in ok.headers   # a real relay is not tagged

    other = await clients.get("/tools/by-name/ghost")                   # not a /call/ path
    assert other.status_code == 404 and "x-treg-error" not in other.headers
