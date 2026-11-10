"""Phase A: treg keeps OAuth tokens fresh. A stale oauth secret is refreshed (via its token_uri)
in place before the call, so the upstream always sees a live token. The upstream's /token route
(conftest) stands in for the provider's token endpoint and returns access_token "REFRESHED".
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from httpx import AsyncClient

from treg import audit, oauth
from treg.domain.identity import api_keys as managed_keys


def _oauth_blob(access: str, expires_at: float) -> str:
    return json.dumps(
        {
            "access_token": access,
            "refresh_token": "RT",
            "client_id": "cid",
            "client_secret": "csec",
            "token_uri": "http://upstream/token",  # ASGITransport routes this to the test upstream
            "expires_at": expires_at,
        }
    )


async def _register_oauth_tool(c: AsyncClient, name: str, blob: str) -> None:
    sid = (await c.post("/secrets", json={"name": f"{name}-s", "kind": "oauth", "value": blob})).json()["id"]
    r = await c.post("/tools", json={"name": name, "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    assert r.status_code == 200, r.text


async def test_stale_token_is_refreshed_before_call(clients: AsyncClient):
    await _register_oauth_tool(clients, "gsc", _oauth_blob("OLD", expires_at=0))  # already expired
    r = await clients.get("/call/gsc/echo")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer REFRESHED"  # treg refreshed it silently


async def test_valid_token_is_not_refreshed(clients: AsyncClient):
    await _register_oauth_tool(clients, "gsc", _oauth_blob("STILL-GOOD", expires_at=time.time() + 3600))
    r = await clients.get("/call/gsc/echo")
    assert r.json()["auth"] == "Bearer STILL-GOOD"  # untouched — still valid


async def test_manual_mode_token_without_refresh_fields_is_injected_as_is(clients: AsyncClient):
    # MANUAL mode: a bare uploaded token (no refresh_token/client creds), even with no expiry,
    # is injected verbatim — treg never tries to refresh what it can't.
    blob = json.dumps({"access_token": "MANUAL-TOKEN"})
    await _register_oauth_tool(clients, "manual", blob)
    r = await clients.get("/call/manual/echo")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer MANUAL-TOKEN"


async def test_token_endpoint_io_holds_no_database_connection(
    clients: AsyncClient, monkeypatch,
):
    """The provider may take seconds; the refresh transaction must release its pool slot first."""
    from treg.api import app
    from treg.infra.db import _engine

    await _register_oauth_tool(clients, "pool-free-refresh", _oauth_blob("OLD", expires_at=0))
    await audit.drain()  # Registration's asynchronous audit write must finish before sampling the pool.
    # SQLite shares this pool with key metadata writes; finish them without clearing the throttle.
    await managed_keys.drain_last_used()
    checked_out: list[int] = []
    original_post = app.state.http.post

    async def _post(*args, **kwargs):
        checked_out.append(_engine.pool.checkedout())
        return await original_post(*args, **kwargs)

    monkeypatch.setattr(app.state.http, "post", _post)
    response = await clients.get("/call/pool-free-refresh/echo")

    assert response.status_code == 200, response.text
    assert checked_out == [0]


async def test_concurrent_stale_calls_keep_one_refresh_winner(
    clients: AsyncClient, monkeypatch,
):
    from treg.api import app

    await _register_oauth_tool(clients, "single-flight-refresh", _oauth_blob("OLD", expires_at=0))
    calls = 0
    first_arrived = asyncio.Event()
    release = asyncio.Event()
    original_post = app.state.http.post

    async def _post(*args, **kwargs):
        nonlocal calls
        calls += 1
        first_arrived.set()
        await release.wait()
        return await original_post(*args, **kwargs)

    monkeypatch.setattr(app.state.http, "post", _post)
    first = asyncio.create_task(clients.get("/call/single-flight-refresh/echo"))
    await first_arrived.wait()
    second = asyncio.create_task(clients.get("/call/single-flight-refresh/echo"))
    await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(first, second)

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["auth"] for response in responses] == [
        "Bearer REFRESHED", "Bearer REFRESHED",
    ]
    assert calls == 1


# ---- token-endpoint client authentication (X / Pinterest demand HTTP Basic) ---------------------
# These drive oauth.refresh() directly against a strict stand-in that behaves like X: a refresh
# posting client_secret in the body is 401'd. The proxy-level tests above can't catch this because
# conftest's /token accepts anything — which is exactly how the bug shipped: connect worked, and
# every refresh died two hours later in production only.


def _x_like_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if request.headers.get("authorization", "").startswith("Basic "):
            assert "client_secret=" not in body  # Basic AND body secret would be double-auth
            calls.append("basic")
            return httpx.Response(200, json={"access_token": "VIA-BASIC", "expires_in": 7200})
        calls.append("body")
        return httpx.Response(401, json={"error": "invalid_client"})  # X's actual behavior
    return httpx.MockTransport(handler)


def _basic_blob(**extra) -> dict:
    return {"access_token": "OLD", "refresh_token": "RT", "client_id": "cid",
            "client_secret": "csec", "token_uri": "https://x.test/2/oauth2/token", **extra}


async def test_refresh_honors_recorded_basic_auth_method():
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_x_like_transport(calls)) as client:
        new = await oauth.refresh(_basic_blob(token_endpoint_auth_method="client_secret_basic"), client)
    assert calls == ["basic"]  # no body-auth attempt at all
    assert new["access_token"] == "VIA-BASIC"


async def test_legacy_blob_without_method_retries_with_basic_and_learns():
    # Connections minted before the method was persisted: first attempt is body auth (401),
    # the retry succeeds with Basic, and the blob records what worked so next time is one call.
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_x_like_transport(calls)) as client:
        new = await oauth.refresh(_basic_blob(), client)
    assert calls == ["body", "basic"]
    assert new["access_token"] == "VIA-BASIC"
    assert new["token_endpoint_auth_method"] == "client_secret_basic"


async def test_refresh_body_auth_providers_unaffected():
    # Google et al: secret in the body, no Authorization header — the pre-fix path, byte for byte.
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert "client_secret=csec" in request.read().decode()
        return httpx.Response(200, json={"access_token": "VIA-BODY", "expires_in": 3600})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        new = await oauth.refresh(_basic_blob(), client)
    assert new["access_token"] == "VIA-BODY"
    assert "token_endpoint_auth_method" not in new  # learned nothing; nothing failed


async def test_exchange_code_persists_basic_auth_method_into_blob():
    # The blob is the ONLY thing refresh() has months later — PendingOAuth is long deleted. If the
    # method doesn't ride along here, every X/Pinterest refresh reverts to body auth and 401s.
    from treg import crypto
    from treg.models import PendingOAuth

    p = PendingOAuth(
        state="s", org_id=1, owner="o@x", provider="x", scopes="tweet.read",
        auth_uri="https://x.test/authorize", token_uri="https://x.test/2/oauth2/token",
        client_id="cid", client_secret=crypto.encrypt("csec"), redirect_uri="https://treg/cb",
        token_endpoint_auth_method="client_secret_basic",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        blob = await oauth.exchange_code(p, "the-code", client)
    assert blob["token_endpoint_auth_method"] == "client_secret_basic"

    # And a Google-shaped pending (default method) must NOT grow the field.
    g = PendingOAuth(
        state="s2", org_id=1, owner="o@x", provider="google-analytics", scopes="r",
        auth_uri="https://g.test/auth", token_uri="https://g.test/token",
        client_id="cid", client_secret=crypto.encrypt("csec"), redirect_uri="https://treg/cb",
    )

    def g_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(g_handler)) as client:
        gblob = await oauth.exchange_code(g, "the-code", client)
    assert "token_endpoint_auth_method" not in gblob


async def _refresh_stored(clients: AsyncClient, monkeypatch, *, provider: str, blob: dict):
    """Store one oauth secret, refresh it through the live path, return (requests, stored blob)."""
    from treg import crypto
    from treg.config import get_settings
    from treg.infra.db import session_maker
    from treg.models import Org, Secret
    from sqlmodel import select

    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "treg-google-cid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "rotated-csec")
    get_settings.cache_clear()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.read().decode()))
        return httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
    try:
        async with session_maker() as db:
            org = (await db.execute(select(Org))).scalars().first()
            row = Secret(org_id=org.id, name="conn", owner="o@example.com", kind="oauth",
                         provider=provider, value=crypto.encrypt(json.dumps({**blob, "expires_at": 0})))
            db.add(row)
            await db.commit()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await oauth.ensure_fresh(row, db, client)
            stored = json.loads(crypto.decrypt((await db.get(Secret, row.id)).value))
    finally:
        get_settings.cache_clear()
    return seen, stored


_EVIL = "https://collector.example/token"


async def test_platform_connection_refreshes_at_the_providers_endpoint_with_current_secret(clients, monkeypatch):
    seen, stored = await _refresh_stored(clients, monkeypatch, provider="google-search-console", blob={
        "access_token": "OLD", "refresh_token": "RT", "client_id": "treg-google-cid",
        "client_secret": "old-csec", "token_uri": _EVIL})
    assert [url for url, _ in seen] == ["https://oauth2.googleapis.com/token"]
    assert "client_secret=rotated-csec" in seen[0][1]
    assert stored["access_token"] == "NEW"
    assert stored["client_secret"] == "old-csec"  # the platform secret is never written into the blob


async def test_a_blob_naming_treg_client_id_without_platform_provenance_never_gets_the_secret(clients, monkeypatch):
    seen, stored = await _refresh_stored(clients, monkeypatch, provider="", blob={
        "access_token": "OLD", "refresh_token": "RT", "client_id": "treg-google-cid",
        "client_secret": "whatever", "token_uri": _EVIL})
    assert [url for url, _ in seen] == [_EVIL]
    assert "rotated-csec" not in seen[0][1]
    assert "rotated-csec" not in json.dumps(stored)
