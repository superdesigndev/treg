"""Per-user token revocation (backlog #4). A signed identity token / session cookie carries the
token_version it was minted at; bumping User.token_version (via POST /auth/revoke-tokens) invalidates
every token the user holds — the kill switch for a leaked token that doesn't disable the account or
log everyone else out. Legacy tokens (minted before the tv claim existed) default to tv=0, so a plain
deploy revokes nobody.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from treg.domain.identity import session as sess
from treg.api import app
from treg.infra.db import reset_db


@pytest.fixture
async def client():
    await reset_db()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://registry",
        headers={"ngrok-skip-browser-warning": "1"},
    ) as c:
        yield c


async def _otp_login(c: AsyncClient, email: str) -> str:
    code = (await c.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    return (await c.post("/auth/email/verify", json={"email": email, "code": code})).json()["token"]


async def test_revoke_kills_old_identity_token_and_issues_a_fresh_one(client):
    tok = await _otp_login(client, "leaky@x.io")
    h = {"X-Treg-Token": tok}
    assert (await client.get("/invites/mine", headers=h)).status_code == 200  # works before revoke

    r = await client.post("/auth/revoke-tokens", headers=h)
    assert r.status_code == 200 and r.json()["revoked"] is True
    fresh = r.json()["token"]

    assert (await client.get("/invites/mine", headers=h)).status_code == 401  # old token now dead
    assert (await client.get("/invites/mine", headers={"X-Treg-Token": fresh})).status_code == 200  # fresh works


async def test_revoke_signs_out_other_browser_sessions_but_keeps_the_caller(client):
    code = (await client.post("/auth/email/start", json={"email": "multi@x.io"})).json()["dev_code"]
    verify = await client.post("/auth/email/verify", json={"email": "multi@x.io", "code": code})
    old_cookie = verify.cookies.get("treg_session")
    assert old_cookie

    # A second device holding the SAME (now-to-be-leaked) session cookie.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry",
                           cookies={"treg_session": old_cookie}) as device_b:
        assert (await device_b.get("/invites/mine")).status_code == 200  # both devices valid

        # Device A revokes (cookie-authed); the endpoint re-issues A a fresh cookie in the same response.
        assert (await client.post("/auth/revoke-tokens")).status_code == 200

        assert (await device_b.get("/invites/mine")).status_code == 401  # leaked session is out
        assert (await client.get("/invites/mine")).status_code == 200      # the caller stays in


async def test_revoke_requires_auth(client):
    assert (await client.post("/auth/revoke-tokens")).status_code == 401


def test_legacy_token_without_tv_claim_defaults_to_zero():
    """A legacy session without ``tv`` remains valid for a user still at token_version zero."""
    raw = json.dumps({"uid": 7, "exp": 9999999999}).encode()  # note: no "tv"
    sig = hmac.new(sess._key(), raw, hashlib.sha256).digest()
    legacy = f"{sess._b64(raw)}.{sess._b64(sig)}"
    claims = sess.read_session_claims(legacy)
    assert claims == {"uid": 7, "exp": 9999999999, "tv": 0}


def _legacy_token(**claims) -> str:
    raw = json.dumps(claims, separators=(",", ":")).encode()
    sig = hmac.new(sess._key(), raw, hashlib.sha256).digest()
    return f"{sess._b64(raw)}.{sess._b64(sig)}"


async def test_typed_credentials_and_safe_legacy_boundary_end_to_end(client):
    tok = await _otp_login(client, "early@x.io")
    claims = sess.read_identity_claims(tok)
    assert claims["aud"] == sess.IDENTITY_AUDIENCE
    assert claims["scope"] == sess.BOOTSTRAP_SCOPE and claims["exp"] > int(time.time())
    uid = claims["uid"]

    # New browser sessions never authenticate as bearers, even before their expiry.
    live_session = client.cookies.get(sess.COOKIE)
    expired_session = sess.make_session(uid, ttl=-1)
    for cookie in (live_session, expired_session):
        assert (await client.get(
            "/invites/mine", headers={"X-Treg-Token": cookie},
        )).status_code == 401

    # A new identity token is not a browser session in the opposite direction either.
    client.cookies.set(sess.COOKIE, tok)
    assert (await client.get("/invites/mine")).status_code == 401

    # A launch-era team-pinned copied key is distinguishable by its signed org claim, so it survives
    # the old 30-day exp and still resolves the team as a bare bearer. The org-less shape is
    # indistinguishable from an expired legacy cookie and is deliberately refused.
    created = (await client.post(
        "/orgs", json={"name": "Legacy Team"}, headers={"X-Treg-Token": tok},
    )).json()
    stale_pinned = _legacy_token(
        uid=uid, tv=0, exp=int(time.time()) - 1, org=created["org"],
    )
    stale_orgless = _legacy_token(uid=uid, tv=0, exp=int(time.time()) - 1)
    assert (await client.get(
        "/tools", headers={"X-Treg-Token": stale_pinned},
    )).status_code == 200
    assert (await client.get(
        "/invites/mine", headers={"X-Treg-Token": stale_orgless},
    )).status_code == 401
    assert (await client.get(
        "/invites/mine", headers={"X-Treg-Token": stale_pinned + "x"},
    )).status_code == 401

    # token_version remains the kill switch for permanent and supported legacy identity keys.
    assert (await client.post(
        "/auth/revoke-tokens", headers={"X-Treg-Token": stale_pinned},
    )).status_code == 200
    assert (await client.get(
        "/invites/mine", headers={"X-Treg-Token": stale_pinned},
    )).status_code == 401
