"""TREG_PAUSED_PROVIDERS: a deployment pauses a provider it can no longer serve.

A paused provider leaves search and the connect listing; its calls and connects are refused with a
typed 503 `provider_paused` before any hold, charge or upstream request; its existing connections
are kept untouched, shown as paused, and skipped by the health sweep. Empty pauses nothing.
"""

from __future__ import annotations

import json
import time

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlmodel import select

from treg import audit, crypto, oauth
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import Settings, get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry, Secret, Tool

from test_mcp import _call_tool, mcp_session

GBP = "google-business-profile"
EP = "google-business-profile.accounts"
HOST = "mybusinessaccountmanagement.googleapis.com"


@pytest.fixture
def paused(monkeypatch):
    monkeypatch.setenv("TREG_PAUSED_PROVIDERS", GBP)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _connect_gbp(clients: AsyncClient) -> int:
    """What a registry connect leaves behind: the provider's oauth secret and the tool bound to it."""
    org_id = await _org_id(clients)
    blob = {"access_token": "GTOK", "refresh_token": "RT", "client_id": "cid", "client_secret": "cs",
            "token_uri": "http://upstream/token", "expires_at": time.time() - 60}  # stale: would refresh
    async with session_maker() as db:
        secret = Secret(org_id=org_id, name=GBP, owner="tim@superdesign.dev", kind="oauth",
                        value=crypto.encrypt(json.dumps(blob)), provider=GBP)
        db.add(secret)
        await db.flush()
        db.add(Tool(org_id=org_id, name=GBP, owner="tim@superdesign.dev",
                    base_url=f"https://{HOST}", host=HOST,
                    health_check={"method": "GET", "path": "/v1/accounts"},
                    bindings=[{"secret_id": secret.id, "injector": "oauth", "location": "header",
                               "name": "Authorization", "format": "Bearer {secret}",
                               "secret_field": "access_token"}]))
        await db.commit()
        return secret.id


async def _secret(secret_id: int) -> Secret:
    async with session_maker() as db:
        return (await db.execute(select(Secret).where(Secret.id == secret_id))).scalars().one()


async def _rows(model):
    async with session_maker() as db:
        return (await db.execute(select(model))).scalars().all()


async def test_a_paused_provider_is_hidden_refused_and_its_connections_kept(
        clients: AsyncClient, paused, monkeypatch):
    sid = await _connect_gbp(clients)
    before = await _secret(sid)
    upstream: list[str] = []

    async def relay(request, upstream_url, *args, **kwargs):
        upstream.append(upstream_url)
        raise AssertionError("a paused provider must never be relayed")

    async def refresh(*args, **kwargs):
        upstream.append("refresh")
        raise AssertionError("a paused connection must never be refreshed")

    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(oauth, "ensure_fresh", refresh)

    # Catalog: out of every search, but a held id is told why.
    assert all(ep["provider"] != GBP
               for ep, _ in catalog_store.search("business profile reviews", catalog_store.load(), 100)[0])
    r = await clients.get("/catalog/search", params={"q": "google business profile reviews", "limit": 100})
    assert r.status_code == 200 and f"{GBP}." not in r.text
    r = await clients.get(f"/catalog/endpoints/{EP}")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_paused"
    assert r.json()["detail"]["message"].startswith("Google Business Profile is temporarily paused")
    r = await clients.get(f"/catalog/endpoints/{EP}/access")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_paused"

    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        found = await _call_tool(c, "catalog_search", {"query": "google business profile reviews",
                                                       "limit": 25}, token=token)
        got = await _call_tool(c, "catalog_get", {"endpoint_id": EP}, token=token)
    assert all(not str(row.get("endpoint_id", "")).startswith(f"{GBP}.") for row in found["results"])
    assert got["error"] == "provider_paused" and "paused" in got["detail"]
    clients.headers["X-Treg-Token"] = token  # mcp_session drops it

    # Calls: the catalog id, the provisioned tool, and a URL passthrough to the provider's host.
    for rest in (EP, f"{GBP}/v1/accounts", f"https://{HOST}/v1/accounts"):
        r = await clients.get(f"/call/{rest}")
        assert r.status_code == 503, (rest, r.text)
        assert r.headers["X-Treg-Error"] == "1" and "X-Treg-Cost-Micro" not in r.headers
        detail = r.json()["detail"]
        assert detail["error"] == "provider_paused" and detail["provider"] == GBP, rest
    assert upstream == [], "no upstream request and no token refresh"
    assert await _rows(Hold) == []
    assert {e.kind for e in await _rows(LedgerEntry)} <= {"grant"}, "no reserve, settle or release"
    await audit.drain()
    calls = (await clients.get("/calls")).json()
    assert len(calls) == 3 and {c["refused_by"] for c in calls} == {"paused"}

    # The dashboard learns what is paused, and what to say, from /meta.
    meta = (await clients.get("/meta")).json()["paused_providers"]
    assert meta == {GBP: {"display_name": "Google Business Profile",
                          "message": get_settings().paused_provider_message(GBP, "Google Business Profile")}}

    # Connect: gone from the listing, refused when started anyway.
    assert GBP not in {p["service"] for p in (await clients.get("/oauth/providers")).json()}
    r = await clients.post("/oauth/start", json={"provider": GBP})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_paused"
    r = await clients.post("/oauth/start", json={"provider": GBP, "connection_id": sid})
    assert r.status_code == 503, "a reconnect of the paused connection is refused too"
    r = await clients.get(f"/connections/{sid}/resources")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_paused"

    # Existing connection: listed as paused, skipped by the health sweep, and never changed.
    row = next(c for c in (await clients.get("/connections")).json() if c["id"] == sid)
    assert row["paused"] is True and row["provider_display_name"] == "Google Business Profile"
    assert "existing connection is saved" in row["paused_message"]
    report = (await clients.post("/health/run")).json()
    assert sid not in {s["secret_id"] for s in report["invalid"] + report["expiring"]}
    assert upstream == []
    after = await _secret(sid)
    assert (after.value, after.health_status, after.health_detail, after.last_error) == (
        before.value, before.health_status, before.health_detail, before.last_error)


async def test_lifting_the_pause_restores_everything_with_no_reconnect(clients: AsyncClient, monkeypatch):
    sid = await _connect_gbp(clients)
    assert get_settings().paused_providers_set == frozenset()
    assert (await clients.get("/meta")).json()["paused_providers"] == {}
    r = await clients.get("/catalog/search", params={"q": "google business profile reviews", "limit": 100})
    assert f"{GBP}." in r.text
    assert (await clients.get(f"/catalog/endpoints/{EP}")).status_code == 200
    assert GBP in {p["service"] for p in (await clients.get("/oauth/providers")).json()}
    row = next(c for c in (await clients.get("/connections")).json() if c["id"] == sid)
    assert "paused" not in row
    seen: list[str] = []

    async def relay(request, upstream_url, *args, **kwargs):
        seen.append(upstream_url)

        async def body():
            yield b'{"accounts":[]}'

        async def close():
            return None
        return UpstreamResponse(200, (("content-type", "application/json"),), body(), close)

    async def fresh(*args, **kwargs):
        return None

    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(oauth, "ensure_fresh", fresh)
    assert (await clients.get(f"/call/{GBP}/v1/accounts")).status_code == 200
    assert seen == [f"https://{HOST}/v1/accounts"], "the same connection is used again"


async def test_a_custom_message_replaces_the_default(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_PAUSED_PROVIDERS", f" {GBP.upper()} , ")
    monkeypatch.setenv("TREG_PAUSED_PROVIDER_MESSAGES", json.dumps({GBP: "Approval is pending."}))
    get_settings.cache_clear()
    try:
        r = await clients.get(f"/call/{EP}")
        assert r.status_code == 503 and r.json()["detail"]["message"] == "Approval is pending."
        assert r.json()["detail"]["endpoint_id"] == EP
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("raw", ["not json", "[]", '{"x": 1}', '{"x": " "}'])
def test_malformed_messages_fail_at_boot(raw):
    with pytest.raises(ValidationError):
        Settings(paused_provider_messages=raw)

