"""Review blockers: an org block refuses before the auth DB query (Bearer and MCP callers too),
MCP streams have their own overload cap, and the client IP is what uvicorn trusts."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

import treg.__main__ as treg_main
from treg.api import app
from treg.application.ratelimit import blocklist_state, limiter
from treg.bootstrap_ratelimit import RateLimitMiddleware
from treg.config import get_settings
from treg.infra import db as infra_db
from treg.infra import kv, request_scope
from tests.fake_rate_limit_store import install, install_blocklist


@pytest.fixture(autouse=True)
def _fresh():
    get_settings.cache_clear()
    limiter.reset_for_tests()
    yield
    limiter.reset_for_tests()
    get_settings.cache_clear()


def _enforce_blocklist() -> None:
    """The blocklist's mode lives on its version row; this is what a poll of `enforce_403` loads."""
    install_blocklist(version=1, mode="enforce_403")


async def _block_org(monkeypatch, org_id: int) -> None:
    install_blocklist(("org_id", str(org_id), 3600), version=3, mode="enforce_403")


@contextmanager
def _db_checkouts():
    """Every connection handed out by any pool while the block runs."""
    seen: list[str] = []
    engines = {id(e): e for e in (infra_db._engine, infra_db._admin_engine,
                                  infra_db._background_engine, infra_db._ratelimit_engine)}

    def checkout(*_):
        seen.append("checkout")

    for engine in engines.values():
        event.listen(engine.sync_engine, "checkout", checkout)
    try:
        yield seen
    finally:
        for engine in engines.values():
            event.remove(engine.sync_engine, "checkout", checkout)


async def _org_of(client) -> int:
    r = await client.get("/tools")
    assert r.status_code == 200
    rl = limiter.limiter()
    return rl.cache.get(request_scope.key_hash(client.headers["X-Treg-Token"]), now=rl._clock()).org_id


# ---- MUST 1: an org block short-circuits before the auth DB query ---------------------------------
async def test_a_blocked_orgs_second_request_opens_no_db_session(clients, monkeypatch):
    _enforce_blocklist()
    org_id = await _org_of(clients)
    limiter.limiter().cache = type(limiter.limiter().cache)(ttl_s=60, max_size=100)  # a cold process
    await _block_org(monkeypatch, org_id)
    first = await clients.get("/tools")
    assert first.status_code == 403 and first.json()["detail"]["reason"] == "treg_blocklist.org_id"
    with _db_checkouts() as seen:
        second = await clients.get("/tools")
    assert second.status_code == 403 and second.json()["detail"]["reason"] == "treg_blocklist.org_id"
    assert seen == []  # refused from the key cache, before auth


async def test_a_bearer_and_mcp_caller_is_cached_and_refused_before_auth(clients, monkeypatch):
    """MCP clients present the key as a Bearer; their tools reach /call/ in-process. The inner
    request's auth teaches the cache the outer Bearer (the alias), so the outer request is refused
    from then on without touching the database."""
    _enforce_blocklist()
    token = clients.headers["X-Treg-Token"]
    org_id = await _org_of(clients)
    limiter.limiter().cache = type(limiter.limiter().cache)(ttl_s=60, max_size=100)
    await _block_org(monkeypatch, org_id)
    async with AsyncClient(transport=ASGITransport(app=request_scope.internal_app(app, outer_token=token)),
                           base_url="http://treg.internal", headers={"X-Treg-Token": token}) as inner:
        assert (await inner.get("/tools")).status_code == 403  # the MCP tool's /call/, refused by auth
    rl = limiter.limiter()
    assert rl.cache.get(request_scope.key_hash(token), now=rl._clock()).org_id == org_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as outer:
        with _db_checkouts() as seen:
            r = await outer.post("/mcp/", headers={"Authorization": f"Bearer {token}"},
                                 json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "treg_blocklist.org_id"
    assert seen == []


async def test_bearer_keys_are_cached_after_a_served_request(clients):
    token = clients.headers["X-Treg-Token"]
    async with AsyncClient(transport=ASGITransport(app=request_scope.internal_app(app, outer_token=token)),
                           base_url="http://treg.internal", headers={"X-Treg-Token": token}) as inner:
        assert (await inner.get("/tools")).status_code == 200
    facts_key = request_scope.key_hash(token)  # what the middleware derives from `Authorization: Bearer`
    rl = limiter.limiter()
    assert rl.cache.get(facts_key, now=rl._clock()) is not None


async def test_unblocking_takes_effect_without_waiting_for_the_key_cache(clients, monkeypatch):
    _enforce_blocklist()
    org_id = await _org_of(clients)
    await _block_org(monkeypatch, org_id)
    assert (await clients.get("/tools")).status_code == 403
    install_blocklist(version=4, mode="enforce_403")  # what the poll loads after an unblock: a newer list without the org
    assert (await clients.get("/tools")).status_code == 200


# ---- MUST 2: MCP streams have their own overload cap ------------------------------------------------
@pytest.fixture
def capped(monkeypatch):
    monkeypatch.setenv("TREG_OVERLOAD_CAP_ENABLED", "true")
    monkeypatch.setenv("TREG_OVERLOAD_CAP_SIZE", "1")
    monkeypatch.setenv("TREG_OVERLOAD_CAP_STREAM_SIZE", "1")
    get_settings.cache_clear()


class Hold:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await self.release.wait()
        await send({"type": "http.response.body", "body": b""})


async def _call(mw, method: str, path: str, accept: str = "") -> int:
    sent: list = []

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": method, "path": path, "client": ("127.0.0.1", 1),
             "headers": [(b"accept", accept.encode())] if accept else []}
    await mw(scope, None, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start"), sent


async def test_mcp_streams_never_take_a_main_permit(capped):
    hold = Hold()
    mw = RateLimitMiddleware(hold)
    main = asyncio.create_task(_call(mw, "POST", "/call/x"))
    await asyncio.sleep(0)
    assert mw._overload.locked()  # the main cap (size 1) is full
    stream = asyncio.create_task(_call(mw, "GET", "/mcp/", MCP_ACCEPT))
    await asyncio.sleep(0)
    assert not stream.done() and mw._stream_cap.locked()  # admitted on its own cap
    status, sent = await _call(mw, "POST", "/call/y")
    assert status == 503 and dict(sent[0]["headers"])[b"x-treg-overload-reason"] == b"overload_cap"
    hold.release.set()
    await asyncio.gather(main, stream)


MCP_ACCEPT = "application/json, text/event-stream"  # what MCP clients send on every request


async def test_a_full_stream_cap_refuses_streams_only(capped):
    hold = Hold()
    mw = RateLimitMiddleware(hold)
    held = asyncio.create_task(_call(mw, "GET", "/mcp/v2/", MCP_ACCEPT))
    await asyncio.sleep(0)
    status, sent = await _call(mw, "GET", "/mcp/", MCP_ACCEPT)
    assert status == 503
    assert json.loads(sent[1]["body"])["detail"]["reason"] == "overload_cap_stream"
    hold.release.set()
    await held
    hold.release = asyncio.Event()
    hold.release.set()
    assert (await _call(mw, "POST", "/call/x"))[0] == 200  # the main cap was never touched
    assert mw._stream_cap._value == 1 and mw._overload._value == 1


async def test_an_mcp_post_accepting_event_stream_takes_the_main_permit(capped):
    hold = Hold()
    mw = RateLimitMiddleware(hold)
    held = asyncio.create_task(_call(mw, "POST", "/mcp/", MCP_ACCEPT))
    await asyncio.sleep(0)
    assert mw._overload.locked() and mw._stream_cap is None
    status, sent = await _call(mw, "POST", "/mcp/", MCP_ACCEPT)
    assert status == 503 and json.loads(sent[1]["body"])["detail"]["reason"] == "overload_cap"
    hold.release.set()
    await held


async def test_an_mcp_tool_call_holds_one_permit_including_its_internal_call(capped):
    """tools/call reaches /call/ through treg's own app in-process: that inner request must not
    need a second main permit, or a cap of N would fit only N/2 MCP calls (and deadlock at 1)."""
    inner_status: list[int] = []

    async def downstream(scope, receive, send):
        if scope["path"] == "/mcp/":
            # The MCP tool forwards to /call/ in-process, while the outer request holds its permit.
            inner = request_scope.internal_app(mw)
            status, _ = await _call(inner, "POST", "/call/tool")
            inner_status.append(status)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = RateLimitMiddleware(downstream)
    status, _ = await _call(mw, "POST", "/mcp/", MCP_ACCEPT)
    assert status == 200 and inner_status == [200]
    assert mw._overload._value == 1


async def test_a_plain_mcp_post_counts_in_the_main_cap(capped):
    hold = Hold()
    mw = RateLimitMiddleware(hold)
    held = asyncio.create_task(_call(mw, "POST", "/mcp/", "application/json"))
    await asyncio.sleep(0)
    assert mw._overload.locked() and mw._stream_cap is None
    hold.release.set()
    await held


def test_the_stream_cap_is_off_with_the_flag():
    assert get_settings().overload_cap_enabled is False


# ---- #5: the client IP is what uvicorn trusts --------------------------------------------------------
def test_the_server_starts_uvicorn_without_trusting_any_forwarded_header_source(monkeypatch):
    calls: list = []
    monkeypatch.setattr(treg_main.sys, "argv", ["treg"])
    monkeypatch.setattr(treg_main, "_prepare_serve", lambda: asyncio.sleep(0))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    treg_main.main()
    [kwargs] = calls
    assert "forwarded_allow_ips" not in kwargs and "proxy_headers" not in kwargs


def test_uvicorn_trusts_forwarded_for_only_from_loopback_by_default(monkeypatch):
    """With `FORWARDED_ALLOW_IPS` unset, uvicorn's default is loopback only, never `*`: a peer that
    is not loopback keeps its own address whatever `X-Forwarded-For` claims."""
    from uvicorn.config import Config
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    config = Config(app=lambda *_: None)
    assert config.forwarded_allow_ips in ("127.0.0.1", ["127.0.0.1"])
    seen: list = []

    async def inner(scope, receive, send):
        seen.append(scope["client"][0])

    mw = ProxyHeadersMiddleware(inner, trusted_hosts=config.forwarded_allow_ips)
    headers = [(b"x-forwarded-for", b"203.0.113.9")]
    asyncio.run(mw({"type": "http", "client": ("10.1.2.3", 5), "headers": headers}, None, None))
    asyncio.run(mw({"type": "http", "client": ("127.0.0.1", 5), "headers": headers}, None, None))
    assert seen == ["10.1.2.3", "203.0.113.9"]


# ---- the 403 carries a fixed code only ----------------------------------------------------------
async def test_the_blocklist_403_never_reveals_the_operators_reason(clients, monkeypatch):
    from treg import crypto
    import time
    from treg.domain.ratelimit import BlockEntry, Blocklist
    _enforce_blocklist()
    secret_reason = "matched signup burst rule 7 from the surge query"
    blocklist_state.install(Blocklist.build(3, "2026-01-01T00:00:00Z", (BlockEntry(
        "key_hash", crypto.hash_token(clients.headers["X-Treg-Token"]), secret_reason,
        time.time() + 3600, "ops"),), mode="enforce_403"))
    r = await clients.get("/tools")
    assert r.status_code == 403
    assert r.json() == {"detail": {"error": "blocked", "reason": "treg_blocklist.key_hash",
                                   "blocklist_version": 3}}
    assert secret_reason not in r.text and "ops" not in r.text
    assert dict(r.headers)["x-treg-block-reason"] == "treg_blocklist.key_hash"
