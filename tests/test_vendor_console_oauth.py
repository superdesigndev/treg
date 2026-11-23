"""The vendor console: a first-party confidential OAuth client that only learns who signed in.

API vendors apply for a listing in a separate service and sign in there with their treg account.
What these pin: the client exists only while its secret and redirect URI are set; a code needs the
secret to be redeemed; the token comes without a refresh token, so no grant is left behind; the
token is accepted by `GET /vendor-console/me` and nowhere else, and no other token is accepted
there; the identity lists every team with the person's role; a brand-new account gets its first
team at consent, as the dashboard would make it.
"""

from __future__ import annotations

import secrets

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import crypto
from treg.config import get_settings
from treg.domain.identity import mcp_oauth
from treg.infra.db import session_maker
from treg.models import Membership, OAuthRefresh, Org, User

from test_mcp import MCP_HEADERS, mcp_session
from test_mcp_oauth import _new_account_cookie, _pkce, _register, _signed_in

SECRET = "vendor-console-test-secret"
REDIRECT = "https://vendors.example.test/auth/treg/callback"
CLIENT = mcp_oauth.VENDOR_CONSOLE_CLIENT_ID


@pytest.fixture
def console_on(monkeypatch):
    monkeypatch.setenv("TREG_VENDOR_CONSOLE_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("TREG_VENDOR_CONSOLE_REDIRECT_URI", REDIRECT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _params(challenge: str, **over) -> dict:
    return {"client_id": CLIENT, "redirect_uri": REDIRECT, "response_type": "code", "state": "st",
            "code_challenge": challenge, "code_challenge_method": "S256", **over}


async def _code(clients: AsyncClient, **over) -> tuple[str, str]:
    verifier, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={**_params(challenge, **over), "org_id": 0,
                                                     "decision": "allow"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT + "?"), r.text
    return r.headers["location"].split("code=")[1].split("&")[0], verifier


async def _token(clients: AsyncClient, code: str, verifier: str, secret: str = SECRET):
    return await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_id": CLIENT, "client_secret": secret})


async def _me(clients: AsyncClient, token: str):
    return await clients.get("/vendor-console/me", headers={"Authorization": f"Bearer {token}"})


async def test_sign_in_returns_the_person_and_every_team_with_its_role(clients, console_on):
    _, org_id = await _signed_in(clients, "vendor-flow@superdesign.dev")
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "vendor-flow@superdesign.dev"))).scalar_one()
        second = Org(slug="vendor-flow-second", name="Second Co")
        db.add(second)
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=second.id, role="member",
                          token_hash=crypto.hash_token(secrets.token_urlsafe(16))))
        await db.commit()
        second_id = second.id

    code, verifier = await _code(clients)
    tok = await _token(clients, code, verifier)
    assert tok.status_code == 200, tok.text
    body = tok.json()
    assert body["scope"] == mcp_oauth.VENDOR_CONSOLE_SCOPE
    assert "refresh_token" not in body

    me = await _me(clients, body["access_token"])
    assert me.status_code == 200, me.text
    assert me.headers["cache-control"] == "no-store"
    assert me.json()["email"] == "vendor-flow@superdesign.dev"
    assert me.json()["email_verified"] is True
    teams = {t["id"]: t for t in me.json()["teams"]}
    assert teams[org_id]["role"] == "owner"
    assert teams[second_id] == {"id": second_id, "slug": "vendor-flow-second", "name": "Second Co",
                                "role": "member"}


async def test_no_grant_is_left_behind(clients, console_on):
    await _signed_in(clients, "vendor-nogrant@superdesign.dev")
    code, verifier = await _code(clients)
    assert (await _token(clients, code, verifier)).status_code == 200
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "vendor-nogrant@superdesign.dev"))).scalar_one()
        rows = (await db.execute(select(OAuthRefresh).where(OAuthRefresh.user_id == user.id))).scalars().all()
    assert rows == []
    assert (await clients.get("/oauth/grants")).json() == []


@pytest.mark.parametrize("secret", ["", "wrong"])
async def test_a_code_without_the_secret_is_worthless(clients, console_on, secret):
    await _signed_in(clients, f"vendor-secret-{secret or 'none'}@superdesign.dev")
    code, verifier = await _code(clients)
    tok = await _token(clients, code, verifier, secret=secret)
    assert tok.status_code == 401 and tok.json()["error"] == "invalid_client"


async def test_the_client_does_not_exist_without_its_settings(clients, monkeypatch):
    for secret, redirect in (("", REDIRECT), (SECRET, ""), (SECRET, "http://vendors.example.test/cb")):
        monkeypatch.setenv("TREG_VENDOR_CONSOLE_CLIENT_SECRET", secret)
        monkeypatch.setenv("TREG_VENDOR_CONSOLE_REDIRECT_URI", redirect)
        get_settings.cache_clear()
        _, challenge = _pkce()
        r = await clients.get("/oauth/authorize", params=_params(challenge, redirect_uri=redirect or REDIRECT))
        assert r.status_code == 400 and "unknown client_id" in r.text, (secret, redirect, r.text)
    get_settings.cache_clear()


async def test_consent_says_what_it_can_and_cannot_see(clients, console_on):
    await _signed_in(clients, "vendor-consent@superdesign.dev")
    _, challenge = _pkce()
    page = await clients.get("/oauth/authorize", params=_params(challenge))
    assert page.status_code == 200
    assert "Sign in to the treg vendor console" in page.text
    assert "cannot call tools, spend a balance or see your keys" in page.text
    assert 'name="org_id" value="0"' in page.text


async def test_only_the_console_resource_is_issued_to_the_console(clients, console_on):
    await _signed_in(clients, "vendor-resource@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={
        **_params(challenge, resource=mcp_oauth.mcp_resource_url()), "org_id": 0, "decision": "allow"},
        follow_redirects=False)
    assert r.status_code == 302 and "error=invalid_target" in r.headers["location"], r.text


async def test_no_other_client_may_ask_for_the_console_resource(clients, console_on):
    client_id = await _register(clients)
    _, org_id = await _signed_in(clients, "vendor-other@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": mcp_oauth.vendor_console_resource_url(), "org_id": org_id, "decision": "allow"},
        follow_redirects=False)
    assert r.status_code == 302 and "error=invalid_target" in r.headers["location"], r.text


async def test_the_console_token_works_nowhere_else_and_nothing_else_works_on_me(clients, console_on):
    await _signed_in(clients, "vendor-audience@superdesign.dev")
    code, verifier = await _code(clients)
    console_token = (await _token(clients, code, verifier)).json()["access_token"]
    clients.cookies.clear()   # only the bearer speaks from here on
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", headers={**MCP_HEADERS, "Authorization": f"Bearer {console_token}"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 401, r.text
    for path in ("/orgs", "/auth/me", "/table-account"):
        r = await clients.get(path, headers={"Authorization": f"Bearer {console_token}"})
        assert r.status_code in (401, 404), (path, r.status_code)

    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "vendor-audience@superdesign.dev"))).scalar_one()
    mcp_token = mcp_oauth.make_access_token(
        user_id=user.id, org_id=1, scope="treg:read", audience=mcp_oauth.mcp_resource_url(),
        token_version=user.token_version)
    for bearer in (mcp_token, "not-a-token", ""):
        r = await _me(clients, bearer)
        assert r.status_code == 401, bearer


async def test_a_new_account_gets_its_first_team(clients, console_on):
    await _new_account_cookie(clients, "jane@newco-vendor.test")
    _, challenge = _pkce()
    page = await clients.get("/oauth/authorize", params=_params(challenge))
    assert page.status_code == 200 and "Newco Vendor" in page.text
    code, verifier = await _code(clients)
    tok = await _token(clients, code, verifier)
    assert tok.status_code == 200, tok.text
    me = (await _me(clients, tok.json()["access_token"])).json()
    assert [(t["name"], t["role"]) for t in me["teams"]] == [("Newco Vendor", "owner")]


async def test_a_suspended_account_reads_as_signed_out(clients, console_on):
    await _signed_in(clients, "vendor-suspended@superdesign.dev")
    code, verifier = await _code(clients)
    token = (await _token(clients, code, verifier)).json()["access_token"]
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "vendor-suspended@superdesign.dev"))).scalar_one()
        user.suspended = True
        await db.commit()
    assert (await _me(clients, token)).status_code == 401
