"""A caller that hangs up before treg has read its body: a caller-side 499, nothing held or sent."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.api import app
from treg.infra.db import session_maker
from treg.models import Hold


async def _gone_before_body(token: bytes, path: str, query: bytes = b"") -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query, "root_path": "",
        "headers": [(b"host", b"registry"), (b"x-treg-token", token),
                    (b"content-type", b"application/json"), (b"content-length", b"32")],
        "client": ("127.0.0.1", 50000), "server": ("registry", 80),
    }
    await app(scope, receive, send)
    return sent


@pytest.fixture
def upstream_requests():
    seen: list[httpx.Request] = []
    original = app.state.http
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200, json={})))
    yield seen
    app.state.http = original


async def _holds() -> list[Hold]:
    async with session_maker() as db:
        return list((await db.execute(select(Hold))).scalars().all())


async def test_a_catalog_call_is_a_caller_side_499(clients: AsyncClient, call_error_lines,
                                                   upstream_requests):
    await clients.post("/secrets", json={"name": "brightdata", "value": "OWN-KEY"})
    sent = await _gone_before_body(clients.headers["X-Treg-Token"].encode("latin-1"),
                                   "/call/brightdata.linkedin.user.profile",
                                   b"dataset_id=gd_l1viktl72bvl7bjuj0")

    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 499
    (line,) = call_error_lines()
    assert (line["by"], line["error_class"], line["caller_side"], line["kind"]) == (
        "treg", "caller_disconnected", True, "client_disconnected")
    assert upstream_requests == [] and await _holds() == []


async def test_an_own_tool_call_is_a_499_not_a_relay(clients: AsyncClient, call_error_lines,
                                                     upstream_requests):
    sid = (await clients.post("/secrets", json={"name": "gone-k", "value": "SEK"})).json()["id"]
    r = await clients.post("/tools", json={"name": "gone", "base_url": "http://gone.example",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text
    sent = await _gone_before_body(clients.headers["X-Treg-Token"].encode("latin-1"),
                                   "/call/gone/resource")

    assert sent[0]["status"] == 499
    assert [line["kind"] for line in call_error_lines()] == ["client_disconnected"]
    assert upstream_requests == [] and await _holds() == []
