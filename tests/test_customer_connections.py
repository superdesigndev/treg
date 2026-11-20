"""Connections a product's own customers make through the product's team (#1849).

A connection started with `customer` belongs to that customer: only calls tagged with it use it,
it has no tool, and a token pinned to another customer never sees it. `return_to` brings the
browser back to the product, and `X-Treg-Connection` picks one of several accounts.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import AsyncClient
from sqlmodel import select

from treg import audit, crypto
from treg import mcp as _mcp
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import PendingOAuth, Secret

GSC_CALL = "/call/google-search-console.sitemaps"
IG_CALL = "/call/instagram.instagram.user.profile"
IG_PARAMS = {"ig_user_id": "17841400000000000", "fields": "id,username"}
TIKHUB_CALL = "/call/tikhub.tiktok.video.comments"


@pytest.fixture
def treg_google_app(monkeypatch):
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "treg-google-cid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "treg-google-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instagram_apps(monkeypatch):
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_ID", "instagram-cid")
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_SECRET", "instagram-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tikhub_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATFORM-TIKHUB-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _org_id(c: AsyncClient) -> int:
    return (await c.get("/orgs")).json()[0]["org_id"]


async def _agent(c: AsyncClient, name: str, customer: str | None = None) -> dict:
    """Headers for a member agent, pinned to `customer` when one is given."""
    body = {"name": name, "role": "member"}
    if customer:
        body["pinned_tags"] = {"customer": customer}
    r = await c.post(f"/orgs/{await _org_id(c)}/agents", json=body)
    assert r.status_code == 200, r.text
    return {"X-Treg-Token": r.json()["token"]}


def _tag(customer: str) -> dict:
    return {"X-Treg-Meta": f"customer={customer}"}


async def _connect(c: AsyncClient, body: dict, headers: dict | None = None) -> dict:
    started = await c.post("/oauth/start", json=body, headers=headers or {})
    assert started.status_code == 200, started.text
    state = started.json()["state"]
    callback = await c.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    assert callback.status_code == 200, callback.text
    status = (await c.get(f"/oauth/status/{state}")).json()
    assert status["status"] == "done", status
    return status


async def _gsc(c: AsyncClient, token: str, **body) -> int:
    status = await _connect(c, {"provider": "google-search-console", **body})
    await _set_token(status["secret_id"], token)
    return status["secret_id"]


async def _instagram(c: AsyncClient, token: str, **body) -> dict:
    status = await _connect(c, {"provider": "instagram", "capability": "manage", **body})
    await _set_token(status["secret_id"], token)
    return status


async def _set_token(secret_id: int, token: str) -> None:
    async with session_maker() as db:
        secret = await db.get(Secret, secret_id)
        blob = json.loads(crypto.decrypt(secret.value))
        blob["access_token"] = token
        secret.value = crypto.encrypt(json.dumps(blob))
        await db.commit()


async def _secret(secret_id: int) -> Secret:
    async with session_maker() as db:
        return await db.get(Secret, secret_id)


async def _tikhub_secret(c: AsyncClient, name: str, value: str, customer: str | None) -> int:
    """A tikhub key stored for a customer: the resolution rules, on a provider with treg's key."""
    org_id = await _org_id(c)
    async with session_maker() as db:
        secret = Secret(org_id=org_id, name=name, owner="tim@superdesign.dev", kind="env",
                        value=crypto.encrypt(value), provider="tikhub", customer=customer)
        db.add(secret)
        await db.commit()
        return secret.id


# ---- starting a connect ------------------------------------------------------------------------
async def test_a_connect_without_customer_stays_the_teams(clients: AsyncClient, treg_google_app):
    sid = await _gsc(clients, "TEAM")
    assert (await _secret(sid)).customer is None
    rows = {row["id"]: row for row in (await clients.get("/connections")).json()}
    assert rows[sid]["customer"] is None
    assert "google-search-console" in {t["name"] for t in (await clients.get("/tools")).json()}


async def test_a_customer_connect_records_its_customer_and_makes_no_tool(
    clients: AsyncClient, treg_google_app,
):
    sid = await _gsc(clients, "A-TOKEN", customer="cust_A")
    assert (await _secret(sid)).customer == "cust_A"
    rows = {row["id"]: row for row in (await clients.get("/connections")).json()}
    assert rows[sid]["customer"] == "cust_A"
    assert (await clients.get("/tools")).json() == []


@pytest.mark.parametrize("value", ["a@b.co", "two words", "x" * 129])
async def test_the_customer_value_follows_the_tag_rule(clients: AsyncClient, treg_google_app, value):
    r = await clients.post("/oauth/start", json={"provider": "google-search-console", "customer": value})
    assert r.status_code == 422, r.text
    async with session_maker() as db:
        assert (await db.execute(select(PendingOAuth))).scalars().all() == []


async def test_a_pinned_token_connects_only_for_its_customer(clients: AsyncClient, treg_google_app):
    pinned = await _agent(clients, "cust-a-bot", "cust_A")
    other = await clients.post("/oauth/start", json={
        "provider": "google-search-console", "customer": "cust_B"}, headers=pinned)
    assert other.status_code == 403, other.text

    status = await _connect(clients, {"provider": "google-search-console"}, headers=pinned)
    assert (await _secret(status["secret_id"])).customer == "cust_A"
    assert status["customer"] == "cust_A"


async def test_a_reconnect_keeps_the_customer_and_needs_that_customers_token(
    clients: AsyncClient, treg_google_app,
):
    sid = await _gsc(clients, "A-TOKEN", customer="cust_A")
    team = await _gsc(clients, "TEAM")
    body = {"provider": "google-search-console", "connection_id": sid}

    refused = await clients.post("/oauth/start", json=body, headers=await _agent(clients, "b", "cust_B"))
    assert refused.status_code == 404, refused.text
    moved = await clients.post("/oauth/start", json={**body, "customer": "cust_B"})
    assert moved.status_code == 422, moved.text
    claimed = await clients.post("/oauth/start", json={
        "provider": "google-search-console", "connection_id": team, "customer": "cust_A"})
    assert claimed.status_code == 422, claimed.text

    again = await _connect(clients, body, headers=await _agent(clients, "a", "cust_A"))
    assert again["secret_id"] == sid
    assert (await _secret(sid)).customer == "cust_A"
    bound = {b["secret_id"] for t in (await clients.get("/tools")).json() for b in t["bindings"]}
    assert bound == {team}


async def test_a_customer_connection_cannot_be_bound_into_a_tool(clients: AsyncClient, treg_google_app):
    sid = await _gsc(clients, "A-TOKEN", customer="cust_A")
    r = await clients.post("/tools", json={
        "name": "leak", "base_url": "https://searchconsole.googleapis.com", "secret_id": sid})
    assert r.status_code == 422, r.text
    assert (await clients.get("/tools")).json() == []


# ---- which calls see it ------------------------------------------------------------------------
async def test_only_the_tagged_customer_uses_its_connection(clients: AsyncClient, treg_google_app):
    """A provider with no grant methods: the customer's beats the team's own tool, and an untagged
    call or another customer's call gets the team's, never A's."""
    await _gsc(clients, "TEAM")
    await _gsc(clients, "A-TOKEN", customer="cust_A")

    def site(n: int) -> dict:
        return {"siteUrl": f"sc-domain:example{n}.com"}

    a = await clients.get(GSC_CALL, params=site(1), headers=_tag("cust_A"))
    assert a.status_code == 200, a.text
    assert a.json()["auth"] == "Bearer A-TOKEN"
    for n, headers in enumerate(({}, _tag("cust_B"), await _agent(clients, "b", "cust_B")), 2):
        r = await clients.get(GSC_CALL, params=site(n), headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["auth"] == "Bearer TEAM"
    pinned = await clients.get(GSC_CALL, params=site(9), headers=await _agent(clients, "a", "cust_A"))
    assert pinned.json()["auth"] == "Bearer A-TOKEN"


async def test_without_a_team_connection_another_customer_gets_no_credential(
    clients: AsyncClient, treg_google_app,
):
    await _gsc(clients, "A-TOKEN", customer="cust_A")
    for headers in ({}, _tag("cust_B")):
        r = await clients.get(GSC_CALL, params={"siteUrl": "sc-domain:example.com"}, headers=headers)
        assert r.status_code == 404, r.text
        assert "A-TOKEN" not in r.text


async def test_grant_methods_resolve_the_customers_connection_first(clients: AsyncClient, instagram_apps):
    await _instagram(clients, "TEAM-IG")
    await _instagram(clients, "A-IG", customer="cust_A")
    a = await clients.get(IG_CALL, params=IG_PARAMS, headers=_tag("cust_A"))
    assert a.status_code == 200, a.text
    assert a.json()["auth"] == "Bearer A-IG"
    for headers in ({}, _tag("cust_B")):
        r = await clients.get(IG_CALL, params=IG_PARAMS, headers=headers)
        assert r.json()["auth"] == "Bearer TEAM-IG"


async def test_grant_methods_never_reach_another_customers_connection(clients: AsyncClient, instagram_apps):
    await _instagram(clients, "A-IG", customer="cust_A")
    for headers in ({}, _tag("cust_B")):
        r = await clients.get(IG_CALL, params=IG_PARAMS, headers=headers)
        assert r.status_code == 428, r.text
        assert r.json()["detail"]["error"] == "authorization_missing"


async def test_the_team_connection_serves_a_customer_with_none(clients: AsyncClient, instagram_apps):
    await _instagram(clients, "TEAM-IG")
    r = await clients.get(IG_CALL, params=IG_PARAMS, headers=_tag("cust_A"))
    assert r.json()["auth"] == "Bearer TEAM-IG"


async def test_treg_key_still_serves_a_customer_without_a_key(clients: AsyncClient, tikhub_platform):
    """A customer's own key is never metered; another customer's call falls to treg's key."""
    await _tikhub_secret(clients, "tikhub-cust-a", "A-KEY", "cust_A")
    a = await clients.get(TIKHUB_CALL, params={"aweme_id": "1"}, headers=_tag("cust_A"))
    assert a.status_code == 200, a.text
    assert a.json()["auth"] == "Bearer A-KEY"
    assert "x-treg-cost-micro" not in a.headers
    for n, headers in enumerate(({}, _tag("cust_B")), 2):
        r = await clients.get(TIKHUB_CALL, params={"aweme_id": str(n)}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["auth"] == "Bearer PLATFORM-TIKHUB-KEY"
        assert int(r.headers["x-treg-cost-micro"]) > 0
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert [row["credential_tier"] for row in rows[:3]] == ["platform", "platform", "credential"]


async def test_the_customers_key_beats_a_team_key_of_the_same_provider(clients: AsyncClient):
    await _tikhub_secret(clients, "tikhub", "TEAM-KEY", None)
    await _tikhub_secret(clients, "tikhub-cust-a", "A-KEY", "cust_A")
    a = await clients.get(TIKHUB_CALL, params={"aweme_id": "1"}, headers=_tag("cust_A"))
    assert a.json()["auth"] == "Bearer A-KEY"
    team = await clients.get(TIKHUB_CALL, params={"aweme_id": "2"}, headers=_tag("cust_B"))
    assert team.json()["auth"] == "Bearer TEAM-KEY"


# ---- several accounts, and naming one --------------------------------------------------------
async def test_two_accounts_of_one_customer_need_a_name(clients: AsyncClient, instagram_apps):
    first = await _instagram(clients, "A-ONE", customer="cust_A")
    second = await _instagram(clients, "A-TWO", customer="cust_A")
    r = await clients.get(IG_CALL, params=IG_PARAMS, headers=_tag("cust_A"))
    assert r.status_code == 409, r.text
    names = {s["secret_id"]: (await _secret(s["secret_id"])).name for s in (first, second)}
    detail = r.json()["detail"]
    assert detail["error"] == "connection_ambiguous"
    assert detail["candidates"] == sorted(names.values())
    assert "A-ONE" not in r.text and "A-TWO" not in r.text

    chosen = await clients.get(IG_CALL, params=IG_PARAMS, headers={
        **_tag("cust_A"), "X-Treg-Connection": names[second["secret_id"]]})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["auth"] == "Bearer A-TWO"
    assert "x-treg-connection" not in chosen.json()["headers"]


async def test_two_team_accounts_keep_newest_wins(clients: AsyncClient, instagram_apps):
    await _instagram(clients, "TEAM-OLD")
    await _instagram(clients, "TEAM-NEW")
    r = await clients.get(IG_CALL, params=IG_PARAMS)
    assert r.status_code == 200, r.text


async def test_a_name_this_call_may_not_use_is_404_with_no_fallback(
    clients: AsyncClient, instagram_apps, treg_google_app,
):
    await _instagram(clients, "TEAM-IG")
    a = await _instagram(clients, "A-IG", customer="cust_A")
    gsc = await _gsc(clients, "GSC")
    a_name = (await _secret(a["secret_id"])).name
    cases = [
        ({"X-Treg-Connection": "nope"}),
        ({**_tag("cust_B"), "X-Treg-Connection": a_name}),
        ({"X-Treg-Connection": a_name}),
        ({"X-Treg-Connection": (await _secret(gsc)).name}),
    ]
    for headers in cases:
        r = await clients.get(IG_CALL, params=IG_PARAMS, headers=headers)
        assert r.status_code == 404, (headers, r.text)
        assert r.json()["detail"]["error"] == "connection_not_found"
        assert "A-IG" not in r.text and "TEAM-IG" not in r.text


async def test_a_name_picks_the_teams_own_connection_too(clients: AsyncClient, instagram_apps):
    team = await _instagram(clients, "TEAM-IG")
    await _instagram(clients, "A-IG", customer="cust_A")
    r = await clients.get(IG_CALL, params=IG_PARAMS, headers={
        **_tag("cust_A"), "X-Treg-Connection": (await _secret(team["secret_id"])).name})
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer TEAM-IG"


async def test_the_access_view_answers_as_the_call_would(clients: AsyncClient, instagram_apps):
    a = await _instagram(clients, "A-IG", customer="cust_A")
    path = "/catalog/endpoints/instagram.instagram.user.profile/access"
    tagged = (await clients.get(path, headers=_tag("cust_A"))).json()
    assert tagged["tier"] == "credential"
    assert tagged["connection"] == (await _secret(a["secret_id"])).name
    assert (await clients.get(path)).json()["tier"] == "none"
    missing = await clients.get(path, headers={"X-Treg-Connection": "nope"})
    assert missing.status_code == 404


async def test_the_mcp_call_forwards_its_connection_argument(monkeypatch):
    captured: dict = {}

    class _Client:
        headers: dict = {}

        async def request(self, method, path, **kwargs):
            captured.update(method=method, path=path, **kwargs)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, path))

    @asynccontextmanager
    async def _api(_token):
        yield _Client()

    async def _org(_client):
        return 1, "team", None

    monkeypatch.setattr(_mcp, "_api", _api)
    monkeypatch.setattr(_mcp, "_resolve_org", _org)
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    await _mcp.call("instagram.instagram.user.profile", params=IG_PARAMS, connection="instagram-2",
                    headers={"X-Treg-Connection": "from-the-model-headers"}, ctx=ctx)
    sent = {k.lower(): v for k, v in captured["headers"].items()}
    assert sent["x-treg-connection"] == "instagram-2"


# ---- listings and management -------------------------------------------------------------------
async def test_a_pinned_token_lists_the_team_and_its_own_customer_only(
    clients: AsyncClient, treg_google_app,
):
    team = await _gsc(clients, "TEAM")
    a = await _gsc(clients, "A", customer="cust_A")
    b = await _gsc(clients, "B", customer="cust_B")
    pinned_a = await _agent(clients, "a", "cust_A")

    def ids(rows):
        return {row["id"] for row in rows}

    assert ids((await clients.get("/connections", headers=pinned_a)).json()) == {team, a}
    assert ids((await clients.get("/connections")).json()) == {team, a, b}
    secrets = {row["id"] for row in (await clients.get("/secrets", headers=pinned_a)).json()}
    assert b not in secrets and a in secrets
    assert b not in {row["secret_id"] for row in (await clients.get("/health", headers=pinned_a)).json()}

    for request in (clients.delete(f"/connections/{b}", headers=pinned_a),
                    clients.patch(f"/connections/{b}", json={"name": "taken-over"}, headers=pinned_a)):
        assert (await request).status_code == 404
    assert (await _secret(b)) is not None


# ---- coming back to the product ----------------------------------------------------------------
async def _origins(c: AsyncClient, origins, headers: dict | None = None):
    return await c.patch(f"/orgs/{await _org_id(c)}", json={"oauth_return_origins": origins},
                         headers=headers or {})


async def test_only_an_admin_sets_the_return_origins(clients: AsyncClient):
    member = await _origins(clients, ["https://app.example.com"], await _agent(clients, "m"))
    assert member.status_code == 403
    r = await _origins(clients, ["https://App.Example.com:443", "https://app.example.com/"])
    assert r.status_code == 200, r.text
    assert r.json()["oauth_return_origins"] == ["https://app.example.com"]
    org = await _org_id(clients)
    assert (await clients.get(f"/orgs/{org}/settings")).json()["oauth_return_origins"] == [
        "https://app.example.com"]
    assert (await _origins(clients, [])).json()["oauth_return_origins"] == []


@pytest.mark.parametrize("origin", [
    "http://app.example.com", "https://app.example.com/back", "https://app.example.com?x=1",
    "ftp://app.example.com", "https://user@app.example.com", "app.example.com",
    "http://localhost:3000",
])
async def test_a_return_origin_is_an_exact_https_origin(clients: AsyncClient, origin):
    assert (await _origins(clients, [origin])).status_code == 400


async def test_a_loopback_origin_is_allowed_only_on_a_local_registry(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "http://localhost:8000")
    get_settings.cache_clear()
    try:
        r = await _origins(clients, ["http://localhost:3000"])
        assert r.status_code == 200, r.text
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("return_to", [
    "http://app.example.com/back", "https://evil.example.com/back",
    "https://app.example.com.evil.com/back", "https://app.example.com/back#frag",
    "javascript:alert(1)",
])
async def test_start_refuses_a_return_to_off_the_list(clients: AsyncClient, treg_google_app, return_to):
    await _origins(clients, ["https://app.example.com"])
    r = await clients.post("/oauth/start", json={"provider": "google-search-console",
                                                 "return_to": return_to})
    assert r.status_code == 400, r.text


async def test_without_origins_no_return_to_is_accepted(clients: AsyncClient, treg_google_app):
    r = await clients.post("/oauth/start", json={"provider": "google-search-console",
                                                 "return_to": "https://app.example.com/back"})
    assert r.status_code == 400, r.text


async def test_the_callback_returns_to_the_product_with_the_state_only(
    clients: AsyncClient, treg_google_app,
):
    await _origins(clients, ["https://app.example.com"])
    started = await clients.post("/oauth/start", json={
        "provider": "google-search-console", "customer": "cust_A",
        "return_to": "https://app.example.com/connected?tab=social&treg_status=forged"})
    assert started.status_code == 200, started.text
    state = started.json()["state"]
    r = await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    assert r.status_code == 302, r.text
    location = urlsplit(r.headers["location"])
    assert (location.scheme, location.netloc, location.path) == ("https", "app.example.com", "/connected")
    assert parse_qs(location.query) == {"tab": ["social"], "treg_state": [state], "treg_status": ["done"]}
    assert not location.fragment
    assert (await clients.get(f"/oauth/status/{state}")).json()["status"] == "done"

    again = await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    assert again.status_code == 302
    assert parse_qs(urlsplit(again.headers["location"]).query)["treg_status"] == ["done"]


async def test_a_failed_consent_returns_to_the_product_as_an_error(clients: AsyncClient, treg_google_app):
    await _origins(clients, ["https://app.example.com"])
    started = await clients.post("/oauth/start", json={
        "provider": "google-search-console", "return_to": "https://app.example.com/back"})
    state = started.json()["state"]
    r = await clients.get(f"/oauth/callback?error=access_denied&state={state}")
    assert r.status_code == 302, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query == {"treg_state": [state], "treg_status": ["error"]}
    assert "access_denied" not in r.headers["location"]


async def test_without_return_to_the_callback_page_is_unchanged(clients: AsyncClient, treg_google_app):
    started = await clients.post("/oauth/start", json={"provider": "google-search-console"})
    r = await clients.get(f"/oauth/callback?code=AUTHCODE&state={started.json()['state']}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---- the schema --------------------------------------------------------------------------------
def test_the_migration_adds_nullable_columns_only():
    path = Path(__file__).parents[1] / "src/treg/alembic/versions/0119_customer_connections.py"
    spec = importlib.util.spec_from_file_location("customer_connections_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    for table in ("secret", "pendingoauth", "org"):
        sa.Table(table, metadata, sa.Column("id", sa.Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(sa.text("INSERT INTO secret (id) VALUES (1)"))
        migration.op = Operations(MigrationContext.configure(db))
        migration.upgrade()
        columns = {table: {c["name"]: c for c in sa.inspect(db).get_columns(table)}
                   for table in ("secret", "pendingoauth", "org")}
        assert db.execute(sa.text("SELECT customer FROM secret")).scalar_one() is None
    assert columns["secret"]["customer"]["nullable"]
    assert {"customer", "return_to"} <= set(columns["pendingoauth"])
    assert columns["org"]["oauth_return_origins"]["nullable"]
