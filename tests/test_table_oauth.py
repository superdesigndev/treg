""""Sign in with treg" for the Google Sheets add-on: the first-party OAuth client `treg-sheets`.

The owner's two decisions (2026-09-26): the token works ONLY on the table routes (`/table/*`,
`/table-columns/*`, `/table-account`), and it belongs to the person, so each request picks one of
their teams with `X-Treg-Org`, checked against membership every time. The mechanism:
docs/context/architecture/table.md.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from test_mcp import _call_tool, mcp_session
from test_mcp_oauth import _grant, _pkce, _register, _signed_in
from tests.test_marketplace_call import _fake_relay, platform_on  # noqa: F401 - tier 4 on
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.identity import mcp_oauth

REDIRECT = "https://script.google.com/macros/d/TESTSCRIPT/usercallback"
EP = "tikhub.tiktok.video.comments"


@pytest.fixture
def sheets_on(monkeypatch):
    monkeypatch.setenv("TREG_TABLE_ENABLED", "1")
    monkeypatch.setenv("TREG_SHEETS_REDIRECT_URIS", REDIRECT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _params(challenge: str, **over) -> dict:
    return {"client_id": mcp_oauth.SHEETS_CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
            "scope": mcp_oauth.SHEETS_SCOPE, "code_challenge": challenge, "code_challenge_method": "S256",
            "resource": mcp_oauth.sheets_resource_url(), **over}


async def _sheets_grant(clients: AsyncClient, email: str) -> tuple[dict, int]:
    """The add-on's whole flow; returns the token answer and the person's first team."""
    _, org_id = await _signed_in(clients, email)
    verifier, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={**_params(challenge), "org_id": 0, "decision": "allow"},
                           follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT), r.text
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": mcp_oauth.SHEETS_CLIENT_ID, "code_verifier": verifier,
        "resource": mcp_oauth.sheets_resource_url()})
    assert tok.status_code == 200, tok.text
    return tok.json(), org_id


async def _make_team(clients: AsyncClient, name: str) -> str:
    """A team made by the person signed in by cookie (the test client also carries another user's
    default team key, which would otherwise own the new team)."""
    key = clients.headers.pop("X-Treg-Token", None)
    try:
        r = await clients.post("/orgs", json={"name": name})
        assert r.status_code in (200, 201), r.text
        return next(o["slug"] for o in (await clients.get("/orgs")).json() if o["name"] == name)
    finally:
        if key is not None:
            clients.headers["X-Treg-Token"] = key


def _bearer(token: str, team: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if team:
        h["X-Treg-Org"] = team
    return h


async def _no_cookie(clients: AsyncClient):
    """The add-on has no browser session: drop the test client's cookie and token headers."""
    clients.cookies.clear()
    clients.headers.pop("X-Treg-Token", None)


async def test_the_consent_page_says_what_sheets_may_do_and_asks_no_team(clients: AsyncClient, sheets_on):
    await _signed_in(clients, "consent@superdesign.dev")
    _, challenge = _pkce()
    page = await clients.get("/oauth/authorize", params=_params(challenge))
    assert page.status_code == 200
    assert "treg for Sheets wants to use your treg account" in page.text
    assert "Call treg tools for you" in page.text and "See your teams and their balances" in page.text
    assert "<select" not in page.text                                  # no team picker


async def test_one_sign_in_reaches_every_team_of_the_person(clients: AsyncClient, sheets_on, platform_on, monkeypatch):
    tok, first_org = await _sheets_grant(clients, "sheets@superdesign.dev")
    second = {"slug": await _make_team(clients, "sheets-second")}
    await _no_cookie(clients)
    access = tok["access_token"]
    assert tok["scope"] == mcp_oauth.SHEETS_SCOPE and tok["refresh_token"]

    account = (await clients.get("/table-account", headers=_bearer(access))).json()
    slugs = [t["slug"] for t in account["teams"]]
    assert account["email"] == "sheets@superdesign.dev" and len(slugs) == 2 and second["slug"] in slugs
    assert all("balance_micro" in t and "role" in t for t in account["teams"])

    cols = await clients.get("/table-columns/treg.people.email.verify", headers=_bearer(access, second["slug"]))
    assert cols.status_code == 200 and cols.json()["column_source"] == "contract"

    seen: list = []
    real = _fake_relay(200, b'{"data": [{"a": 1}]}')

    async def spy(request, *a, **kw):
        seen.append({k.lower() for k, _ in request.raw_headers})
        return await real(request, *a, **kw)
    monkeypatch.setattr(call_service, "relay", spy)
    first_slug = next(t["slug"] for t in account["teams"] if t["org_id"] == first_org)
    r = await clients.get(f"/table/{EP}?aweme_id=1", headers=_bearer(access, first_slug))
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [[1]]
    assert seen and b"authorization" not in seen[0]                    # treg's token never reaches the provider


async def test_a_team_the_person_is_not_in_is_refused(clients: AsyncClient, sheets_on):
    tok, _ = await _sheets_grant(clients, "outsider@superdesign.dev")
    await _signed_in(clients, "someone-else@superdesign.dev")
    other = await _make_team(clients, "not-yours")
    await _no_cookie(clients)
    r = await clients.get("/table-account", headers=_bearer(tok["access_token"], other))
    assert r.status_code in (403, 404)


async def test_the_sheets_token_works_nowhere_else(clients: AsyncClient, sheets_on):
    tok, org_id = await _sheets_grant(clients, "narrow@superdesign.dev")
    await _no_cookie(clients)
    access = tok["access_token"]
    assert (await clients.get("/orgs", headers=_bearer(access))).status_code == 401
    assert (await clients.get(f"/call/{EP}?aweme_id=1", headers=_bearer(access))).status_code == 401
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=access)
    assert "balance_usd" not in str(out)


async def test_an_mcp_token_does_not_work_on_the_table_routes(clients: AsyncClient, sheets_on):
    access, _ = await _grant(clients, "mcp-user@superdesign.dev")
    await _no_cookie(clients)
    assert (await clients.get("/table-account", headers=_bearer(access))).status_code == 401


async def test_only_the_sheets_client_gets_the_table_resource(clients: AsyncClient, sheets_on):
    other = await _register(clients)
    await _signed_in(clients, "resource@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", follow_redirects=False, params={
        **_params(challenge), "client_id": other, "redirect_uri": "https://client.test/cb"})
    assert r.status_code == 302 and "invalid_target" in r.headers["location"]
    r = await clients.get("/oauth/authorize", follow_redirects=False,
                          params=_params(challenge, resource=mcp_oauth.mcp_resource_url()))
    assert r.status_code == 302 and "invalid_target" in r.headers["location"]


async def test_no_redirect_setting_means_no_client(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_TABLE_ENABLED", "1")
    get_settings.cache_clear()
    await _signed_in(clients, "absent@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params=_params(challenge), follow_redirects=False)
    assert r.status_code == 400 and r.json()["error"] == "invalid_client"
    get_settings.cache_clear()


async def test_refresh_keeps_it_signed_in_and_sign_out_ends_it(clients: AsyncClient, sheets_on):
    tok, _ = await _sheets_grant(clients, "refresh@superdesign.dev")
    await _no_cookie(clients)
    fresh = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": mcp_oauth.SHEETS_CLIENT_ID})
    assert fresh.status_code == 200, fresh.text
    access = fresh.json()["access_token"]
    assert (await clients.get("/table-account", headers=_bearer(access))).status_code == 200
    await clients.post("/oauth/revoke", data={"token": fresh.json()["refresh_token"]})
    again = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": fresh.json()["refresh_token"],
        "client_id": mcp_oauth.SHEETS_CLIENT_ID})
    assert again.status_code == 400
