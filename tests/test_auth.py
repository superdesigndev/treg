"""Phase 1.5 — GitHub OAuth + cookie sessions, and the dual-auth path (session OR X-Treg-Token).

The GitHub endpoints are faked with an in-process ASGI app mounted as the registry's outbound http
client (ASGITransport routes every absolute URL to it), so the callback exchange runs for real.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg import crypto
from treg.domain.identity import session as sess
from treg.api import app
from treg.config import get_settings
from treg.infra.db import reset_db, session_maker
from treg.models import Membership, Org, User


def _github_app() -> FastAPI:
    g = FastAPI()

    @g.post("/login/oauth/access_token")
    async def token() -> dict:
        return {"access_token": "gho_test", "token_type": "bearer"}

    @g.get("/user")
    async def user() -> dict:
        return {"login": "octo", "email": None}  # force the /user/emails path

    @g.get("/user/emails")
    async def emails() -> list:
        return [{"email": "octo@example.com", "primary": True, "verified": True}]

    return g


# ---- session signing (pure) ---------------------------------------------------------------
def test_session_sign_roundtrip_tamper_expiry():
    t = sess.make_session(42)
    assert sess.read_session(t) == 42
    assert sess.read_session(t + "x") is None          # tampered signature
    assert sess.read_session("garbage") is None         # malformed
    assert sess.read_session(sess.make_session(1, ttl=-1)) is None  # expired


def _legacy_token(**claims) -> str:
    raw = json.dumps(claims, separators=(",", ":")).encode()
    sig = hmac.new(sess._key(), raw, hashlib.sha256).digest()
    return f"{sess._b64(raw)}.{sess._b64(sig)}"


def test_new_identity_and_session_audiences_never_cross():
    identity = sess.make_identity(9, token_version=3, org="acme")
    assert sess.read_identity_claims(identity) == {
        "uid": 9, "tv": 3, "org": "acme", "aud": sess.IDENTITY_AUDIENCE,
    }
    assert sess.read_session_claims(identity) is None

    live_session = sess.make_session(9)
    expired_session = sess.make_session(9, ttl=-1)
    expired_bridge_identity = sess.make_identity(9, ttl=-1)
    assert sess.read_session_claims(live_session)["aud"] == sess.SESSION_AUDIENCE
    assert sess.read_identity_claims(live_session) is None
    assert sess.read_session_claims(expired_session) is None
    assert sess.read_identity_claims(expired_session) is None
    assert sess.read_identity_claims(expired_bridge_identity) is None
    assert sess.read_identity_claims(identity + "x") is None  # signature covers the audience too


def test_legacy_compatibility_stops_at_the_cryptographic_boundary():
    """An org claim distinguishes old team-pinned copied keys. An org-less token with an ``exp``
    could instead be a browser cookie, so bearer compatibility ends when that timestamp passes."""
    expired = int(time.time()) - 1
    future = int(time.time()) + 60
    pinned = _legacy_token(uid=9, tv=0, exp=expired, org="acme")
    stale_ambiguous = _legacy_token(uid=9, tv=0, exp=expired)
    live_ambiguous = _legacy_token(uid=9, tv=0, exp=future)
    pr_era_identity = _legacy_token(uid=9, tv=0)

    assert sess.read_identity_claims(pinned)["org"] == "acme"
    assert sess.read_identity_claims(stale_ambiguous) is None
    assert sess.read_identity_claims(live_ambiguous)["uid"] == 9
    assert sess.read_identity_claims(pr_era_identity)["uid"] == 9
    assert sess.read_session_claims(pinned) is None
    assert sess.read_session_claims(pr_era_identity) is None


def test_typed_identity_scopes_round_trip_and_bootstrap_expires():
    bootstrap = sess.read_identity_claims(sess.make_identity(
        7, ttl=sess.BOOTSTRAP_TTL_SECONDS, scope=sess.BOOTSTRAP_SCOPE,
    ))
    assert bootstrap is not None
    assert bootstrap["scope"] == sess.BOOTSTRAP_SCOPE and bootstrap["exp"] > int(time.time())
    default = sess.read_identity_claims(sess.make_identity(
        7, org="acme", key_generation=2, scope=sess.TEAM_SCOPE,
    ))
    assert default == {
        "uid": 7, "tv": 0, "org": "acme", "kg": 2,
        "aud": sess.IDENTITY_AUDIENCE, "scope": sess.TEAM_SCOPE,
    }


_OAUTH_ENV = {
    "github": {
        "TREG_GITHUB_CLIENT_ID": "cid",
        "TREG_GITHUB_CLIENT_SECRET": "csec",
        "TREG_GITHUB_TOKEN_URL": "http://gh/login/oauth/access_token",
        "TREG_GITHUB_API_URL": "http://gh",
    },
    "google": {
        "TREG_GOOGLE_CLIENT_ID": "gid",
        "TREG_GOOGLE_CLIENT_SECRET": "gsec",
        "TREG_GOOGLE_TOKEN_URL": "http://gg/token",
        "TREG_GOOGLE_USERINFO_URL": "http://gg/userinfo",
    },
}


@asynccontextmanager
async def _oauth_client(monkeypatch, provider: str):
    for key, value in _OAUTH_ENV[provider].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TREG_SESSION_SECRET", "test-session-secret")
    get_settings.cache_clear()
    await reset_db()
    upstream, base_url = (_github_app(), "http://gh") if provider == "github" else (_google_app(), "http://gg")
    app.state.http = AsyncClient(transport=ASGITransport(app=upstream), base_url=base_url)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    await app.state.http.aclose()
    get_settings.cache_clear()


@pytest.fixture
async def gc(monkeypatch):
    async with _oauth_client(monkeypatch, "github") as c:
        yield c


@pytest.fixture(params=["github", "google"])
async def login(request, monkeypatch):
    async with _oauth_client(monkeypatch, request.param) as c:
        yield request.param, c


async def test_login_creates_user_session_but_no_auto_org(login):
    provider, c = login
    email = {"github": "octo@example.com", "google": "guser@example.com"}[provider]
    r = await c.get(f"/auth/{provider}", follow_redirects=False)
    assert r.status_code == 302
    authorize_host = {"github": "github.com", "google": "accounts.google.com"}[provider]
    assert urlsplit(r.headers["location"]).hostname == authorize_host
    state = c.cookies.get("treg_oauth_state")
    assert state
    cb = await c.get(f"/auth/{provider}/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 302 and cb.headers["location"] == "/app"
    assert c.cookies.get("treg_session")  # session cookie set (secure omitted over http)
    me = await c.get("/auth/me")
    assert me.status_code == 200 and me.json()["email"] == email
    # first login creates the USER ONLY - no throwaway personal org; the user names their first team next
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert u.email_verified_at is not None
        assert u.signup_promo_available
        n = len((await s.execute(select(Membership).where(Membership.user_id == u.id))).scalars().all())
    assert n == 0


async def test_bad_state_rejected(login):
    provider, c = login
    await c.get(f"/auth/{provider}", follow_redirects=False)
    cb = await c.get(f"/auth/{provider}/callback?code=abc&state=WRONG", follow_redirects=False)
    assert cb.status_code == 400


async def test_no_session_no_token_is_401(gc):
    assert (await gc.get("/auth/me")).status_code == 401
    assert (await gc.get("/tools")).status_code == 401


# ---- dual auth: a session acts in an org via X-Treg-Org -----------------------------------
async def _seed(email="dev@x.dev", role="owner", superadmin=False):
    slug = email.split("@")[0] + "-team"  # unique per user
    async with session_maker() as s:
        u = User(email=email, is_superadmin=superadmin)
        s.add(u)
        await s.flush()
        o = Org(name="Team", slug=slug)
        s.add(o)
        await s.flush()
        s.add(Membership(user_id=u.id, org_id=o.id, role=role, token_hash=crypto.hash_token("tok-"+email)))
        await s.commit()
        return u.id, o.id, slug


async def test_session_scopes_by_x_treg_org(gc):
    uid, oid, slug = await _seed()
    gc.cookies.set("treg_session", sess.make_session(uid))
    # no org header → 400 (must choose)
    assert (await gc.get("/tools")).status_code == 400
    # with the org header → 200, scoped to that org
    assert (await gc.get("/tools", headers={"X-Treg-Org": slug})).status_code == 200
    # a member endpoint works by org id too
    assert (await gc.get("/tools", headers={"X-Treg-Org": str(oid)})).status_code == 200
    # /orgs lists the session user's memberships (no org needed)
    orgs = await gc.get("/orgs")
    assert orgs.status_code == 200 and orgs.json()[0]["slug"] == slug


async def test_browser_session_securely_exchanges_for_the_selected_default(gc):
    uid, _, slug = await _seed(email="exchange@x.dev")
    gc.cookies.set("treg_session", sess.make_session(uid))
    minted = await gc.get("/auth/cli-token", headers={"X-Treg-Org": slug})
    assert minted.status_code == 200, minted.text
    token = minted.json()["token"]
    claims = sess.read_identity_claims(token)
    assert claims["scope"] == sess.TEAM_SCOPE and claims["org"] == slug
    assert (await gc.get("/tools", headers={"X-Treg-Token": token})).status_code == 200


async def test_session_superadmin_reaches_admin(gc):
    uid, _, _ = await _seed(email="root@x.dev", superadmin=True)
    gc.cookies.set("treg_session", sess.make_session(uid))
    assert (await gc.get("/admin/stats")).status_code == 200
    # a non-superadmin session is refused
    uid2, _, _ = await _seed(email="plain@x.dev", superadmin=False)
    gc.cookies.set("treg_session", sess.make_session(uid2))
    assert (await gc.get("/admin/stats")).status_code == 403


async def test_cli_token_without_a_team_mints_a_restricted_bootstrap(clients):
    """An org-less login token identifies the account for onboarding, not a billing team."""
    r = await clients.get("/auth/cli-token")
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    claims = sess.read_identity_claims(tok)
    assert tok and r.json().get("email") and claims["scope"] == sess.BOOTSTRAP_SCOPE
    assert claims["exp"] > int(time.time())
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    # Supplying a team header cannot turn this onboarding token into a team credential.
    ok = await clients.get("/tools", headers={"X-Treg-Token": tok, "X-Treg-Org": slug})
    assert ok.status_code == 403 and "temporary login token" in ok.json()["detail"]
    exchange = await clients.get(
        "/auth/cli-token", headers={"X-Treg-Token": tok, "X-Treg-Org": slug},
    )
    assert exchange.status_code == 403


async def test_cli_token_requires_auth(clients):
    r = await clients.get("/auth/cli-token", headers={"X-Treg-Token": "nope"})
    assert r.status_code == 401


async def test_cli_token_bakes_the_active_org_and_works_as_a_BARE_bearer(clients):
    """The point of this whole change: when the dashboard asks for its API key WITH the active team
    (X-Treg-Org), the returned token pins that team — so it authenticates a call as a BARE bearer,
    no X-Treg-Org header. That is what lets it be pasted into an MCP server's Authorization, where no
    second header can travel, and still resolve to the right team."""
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    r = await clients.get("/auth/cli-token", headers={"X-Treg-Org": slug})
    assert r.status_code == 200 and r.json().get("org") == slug, r.text
    baked = r.json()["token"]
    claims = sess.read_identity_claims(baked)
    assert claims["scope"] == sess.TEAM_SCOPE and "exp" not in claims
    # the baked token works with NO X-Treg-Org — the org rides on the token
    ok = await clients.get("/tools", headers={"X-Treg-Token": baked, "X-Treg-Org": ""})
    assert ok.status_code == 200, ok.text
    mismatch = await clients.get(
        "/tools", headers={"X-Treg-Token": baked, "X-Treg-Org": "another-team"},
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["detail"] == (
        "this key belongs to another team — use this team's Default key; "
        "if you use the treg CLI, run `treg update`, then `treg login`"
    )


async def test_orgs_marks_the_team_pinned_tokens_org_active(gc):
    """GET /orgs must mark active the org baked into a team-pinned identity token — that flag is how
    `treg login --token` lands on the right team. Before this, no org was marked active for such a
    token and the CLI guessed the FIRST membership: for a multi-team user, an arbitrary other team."""
    async with session_maker() as s:
        u = User(email="two-teams@x.dev")
        s.add(u)
        await s.flush()
        first = Org(name="First", slug="first-team")
        second = Org(name="Second", slug="second-team")
        s.add(first)
        s.add(second)
        await s.flush()
        s.add(Membership(user_id=u.id, org_id=first.id, role="owner", token_hash=crypto.hash_token("t1")))
        s.add(Membership(user_id=u.id, org_id=second.id, role="owner", token_hash=crypto.hash_token("t2")))
        await s.commit()
        uid = u.id
    pinned = sess.make_identity(uid, org="second-team")
    r = await gc.get("/orgs", headers={"X-Treg-Token": pinned})
    assert r.status_code == 200, r.text
    assert [o["slug"] for o in r.json() if o["active"]] == ["second-team"], r.json()
    # X-Treg-Org still wins over the claim — the same precedence require_member applies
    r = await gc.get("/orgs", headers={"X-Treg-Token": pinned, "X-Treg-Org": "first-team"})
    assert [o["slug"] for o in r.json() if o["active"]] == ["first-team"], r.json()


async def test_cli_token_refuses_to_pin_a_team_you_are_not_in(clients):
    """The org claim is only baked when the caller is actually a member — a header naming someone
    else's team yields a plain (unpinned) token, never one that pins a team you cannot reach."""
    r = await clients.get("/auth/cli-token", headers={"X-Treg-Org": "some-other-teams-slug"})
    assert r.status_code == 200 and r.json().get("org") is None, r.text
    # and the short-lived bootstrap cannot access team resources
    plain = r.json()["token"]
    assert sess.read_identity_claims(plain)["scope"] == sess.BOOTSTRAP_SCOPE
    assert (await clients.get("/tools", headers={"X-Treg-Token": plain})).status_code == 403


# ---- Google OAuth (a parallel login door) -------------------------------------------------
def _google_app():
    g = FastAPI()

    @g.post("/token")
    async def token() -> dict:
        return {"access_token": "goog_test", "token_type": "bearer"}

    @g.get("/userinfo")
    async def userinfo() -> dict:
        return {"email": "guser@example.com", "email_verified": True}

    return g


async def test_oauth_callback_carries_arena_acquisition_and_counts_signup_once(gc, monkeypatch):
    from treg import analytics
    events = []
    monkeypatch.setattr(analytics, "capture", lambda *a, **k: events.append(a))
    gc.cookies.set("treg_entry_surface", "arena")
    for _ in range(2):
        await gc.get("/auth/github", params={"return_to": "/enrich-arena"})
        state = gc.cookies.get("treg_oauth_state")
        r = await gc.get("/auth/github/callback", params={"code": "test", "state": state})
        assert r.status_code == 302 and r.headers["location"] == "/enrich-arena"
    signups = [a for a in events if a[1] == "signup_completed"]
    assert len(signups) == 1
    assert signups[0][2] == {"signup_method": "github", "entry_surface": "arena"}


async def test_oauth_arena_return_cookie_encrypts_and_restores_query(gc):
    from urllib.parse import urlencode

    target = "/enrich-arena?" + urlencode({
        "run": "saved-run", "team": "sales; Secure\r\nSet-Cookie: injected=1",
    })
    started = await gc.get("/auth/github", params={"return_to": target})
    cookie = gc.cookies.get("treg_arena_return")
    assert cookie != target
    assert crypto.decrypt(cookie) == target
    assert set(gc.cookies.keys()) == {"treg_oauth_state", "treg_arena_return"}
    return_header = next(h for h in started.headers.get_list("set-cookie")
                         if h.startswith("treg_arena_return="))
    assert "HttpOnly" in return_header and "SameSite=lax" in return_header

    state = gc.cookies.get("treg_oauth_state")
    response = await gc.get("/auth/github/callback", params={"code": "test", "state": state})
    assert response.status_code == 302
    assert response.headers["location"] == target
    assert gc.cookies.get("treg_arena_return") is None
