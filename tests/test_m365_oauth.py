""""Microsoft 365 Copilot" - the first-party confidential OAuth client of treg's declarative agent.

Microsoft's token store sends every plugin's authorization code to one shared redirect URI and calls
the token endpoint the way Microsoft's static OAuth registration does: the client id and secret in
an `Authorization: Basic` header (or the form body), PKCE on, and no RFC 8707 `resource`. Because the
redirect URI is shared, anyone can register a plugin that points at our authorize URL with our client
id; the secret is the only thing that makes a code redeemable by treg's own registration.
"""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient

from treg import mcp
from treg.config import get_settings
from treg.domain.identity import mcp_oauth

from test_mcp import _call_tool, mcp_session
from test_mcp_oauth import _pkce, _signed_in

SECRET = "m365-test-secret"
REDIRECT = mcp_oauth.M365_REDIRECT_URI


@pytest.fixture
def m365_on(monkeypatch):
    monkeypatch.setenv("TREG_M365_CLIENT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _basic(client_id: str = mcp_oauth.M365_CLIENT_ID, secret: str = SECRET) -> dict:
    raw = f"{client_id}:{secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _params(challenge: str) -> dict:
    return {"client_id": mcp_oauth.M365_CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
            "scope": "treg:catalog treg:call treg:read", "state": "st", "code_challenge": challenge,
            "code_challenge_method": "S256"}


async def _code(clients: AsyncClient, email: str) -> tuple[str, str, int]:
    _, org_id = await _signed_in(clients, email)
    verifier, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={**_params(challenge), "org_id": org_id,
                                                     "decision": "allow"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT + "?"), r.text
    return r.headers["location"].split("code=")[1].split("&")[0], verifier, org_id


async def test_microsoft_token_store_flow_drives_the_tools(clients, m365_on):
    """Basic client authentication, no `resource`, a refresh, and the token works on `/mcp/`."""
    code, verifier, org_id = await _code(clients, "m365-flow@superdesign.dev")
    tok = await clients.post("/oauth/token", headers=_basic(), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    claims = mcp._oauth_claims(tok.json()["access_token"])
    assert claims is not None and claims["org"] == org_id

    refreshed = await clients.post("/oauth/token", headers=_basic(), data={
        "grant_type": "refresh_token", "refresh_token": tok.json()["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=refreshed.json()["access_token"])
    assert "balance_usd" in out, out


async def test_the_secret_may_travel_in_the_form_body(clients, m365_on):
    code, verifier, _ = await _code(clients, "m365-post@superdesign.dev")
    tok = await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_id": mcp_oauth.M365_CLIENT_ID, "client_secret": SECRET})
    assert tok.status_code == 200, tok.text


@pytest.mark.parametrize("headers,form", [
    ({}, {"client_id": mcp_oauth.M365_CLIENT_ID}),
    (_basic(secret="wrong"), {}),
    ({}, {"client_id": mcp_oauth.M365_CLIENT_ID, "client_secret": "wrong"}),
])
async def test_a_code_without_the_secret_is_worthless(clients, m365_on, headers, form):
    """Someone else's plugin registration can send a user through our consent screen and receive the
    code at Microsoft's shared redirect, but it cannot redeem the code without our secret."""
    code, verifier, _ = await _code(clients, "m365-nosecret@superdesign.dev")
    tok = await clients.post("/oauth/token", headers=headers, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier, **form})
    assert tok.status_code == 401, tok.text
    assert tok.json()["error"] == "invalid_client"
    assert (tok.headers.get("www-authenticate", "").startswith("Basic")) == bool(headers)


async def test_a_refresh_without_the_secret_neither_works_nor_revokes(clients, m365_on):
    code, verifier, _ = await _code(clients, "m365-refresh@superdesign.dev")
    tok = await clients.post("/oauth/token", headers=_basic(), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier})
    first = tok.json()["refresh_token"]
    stolen = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first,
        "client_id": mcp_oauth.M365_CLIENT_ID})
    assert stolen.status_code == 401 and stolen.json()["error"] == "invalid_client"
    ok = await clients.post("/oauth/token", headers=_basic(), data={
        "grant_type": "refresh_token", "refresh_token": first})
    assert ok.status_code == 200, "a refused attempt must not spend or revoke the grant"


async def test_two_client_authentication_methods_at_once_are_refused(clients, m365_on):
    code, verifier, _ = await _code(clients, "m365-both@superdesign.dev")
    tok = await clients.post("/oauth/token", headers=_basic(), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_secret": SECRET})
    assert tok.status_code == 400 and tok.json()["error"] == "invalid_request"


async def test_consent_names_microsoft_and_carries_no_self_registration_warning(clients, m365_on):
    await _signed_in(clients, "m365-consent@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params=_params(challenge))
    assert r.status_code == 200, r.text
    assert "Microsoft 365 Copilot wants to use treg" in r.text
    assert "registered itself" not in r.text
    assert "spends the team's balance" in r.text


async def test_only_microsofts_redirect_is_accepted(clients, m365_on):
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        **_params(challenge), "redirect_uri": "https://attacker.test/cb"}, follow_redirects=False)
    assert r.status_code == 400


async def test_the_client_does_not_exist_without_a_secret(clients):
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params=_params(challenge), follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client"


async def test_a_public_clients_basic_header_is_still_ignored(clients):
    """A public client's code is bound by PKCE alone; a secret it sends changes nothing."""
    r = await clients.post("/oauth/register", json={"client_name": "Public", "redirect_uris": [REDIRECT]})
    client_id = r.json()["client_id"]
    _, org_id = await _signed_in(clients, "m365-public@superdesign.dev")
    verifier, challenge = _pkce()
    approved = await clients.post("/oauth/authorize", data={
        **_params(challenge), "client_id": client_id, "org_id": org_id, "decision": "allow"},
        follow_redirects=False)
    code = approved.headers["location"].split("code=")[1].split("&")[0]
    tok = await clients.post("/oauth/token", headers=_basic(client_id, "anything"), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
