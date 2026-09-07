"""The email-domain blocklist, refused at every door.

A new team is created with a promotional balance, which makes bulk registration on throwaway
addresses worth someone's while. The list is entirely configuration — `TREG_BLOCKED_EMAIL_DOMAINS`,
a dashboard edit so a new domain needs no deploy — and unset means nothing is blocked. Each rule is
pinned here because the obvious implementation gets it wrong: match the DOMAIN only, walk parent
domains but never the bare TLD, refuse sign-in as well as sign-up, cover BOTH doors that create a
promo-funded team, reveal nothing to the caller, count every block in the log, and fail open.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg.api import app
from treg.application import signup
from treg.config import get_settings
from treg.domain.identity.access import _is_blocked_email
from treg.infra.db import reset_db, session_maker
from treg.models import Org, User

# The list under test. `.example` is reserved by RFC 2606 and can never be a real user's domain, so
# these tests can never collide with a customer.
OPS = "farm-a.example, Farm-B.example ,@farm-c.example,.farm-d.example"
REFUSAL = "this address cannot be used to sign in"


@pytest.fixture
def ops(monkeypatch):
    """Set the blocklist on the live Settings object (the shape conftest uses for `posthog_key`)."""
    def _set(raw: str = OPS) -> None:
        monkeypatch.setattr(get_settings(), "blocked_email_domains", raw, raising=False)
    return _set


@pytest.fixture
async def client():
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c


async def _user_count(email: str) -> int:
    async with session_maker() as s:
        return len((await s.execute(select(User).where(User.email == email))).scalars().all())


async def _otp_start(c: AsyncClient, email: str):
    return await c.post("/auth/email/start", json={"email": email})


async def _otp_login(c: AsyncClient, email: str) -> str:
    code = (await _otp_start(c, email)).json()["dev_code"]
    r = await c.post("/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ---- the classifier ------------------------------------------------------------------------------

def test_nothing_is_blocked_with_no_setting_at_all(ops):
    """No list ships in the code, so an unset variable must let every address through. This is the
    default a fresh deploy runs with, and a self-hoster's only state."""
    ops("")
    assert get_settings().blocked_email_domain_set == frozenset()
    for email in ("a@farm-a.example", "a@mail.farm-a.example", "a@anything.test", "a@company.dev"):
        assert not _is_blocked_email(email), email


def test_match_is_on_the_domain_only_never_the_local_part(ops):
    """The single most important rule. Matching the whole address false-flags real people whose
    USERNAME happens to contain a listed string, which is how a blocklist starts refusing
    customers."""
    ops()
    assert not _is_blocked_email("farm-a.example@company.dev")
    assert not _is_blocked_email("farm-a@company.dev")


def test_ops_tier_parses_case_whitespace_and_leading_marks(ops):
    ops()
    assert get_settings().blocked_email_domain_set == frozenset(
        {"farm-a.example", "farm-b.example", "farm-c.example", "farm-d.example"})


def test_a_listed_domain_matches_itself_and_every_subdomain(ops):
    ops()
    assert _is_blocked_email("a@farm-a.example")
    assert _is_blocked_email("A@FARM-B.EXAMPLE")
    assert _is_blocked_email("a@deep.mail.farm-c.example")   # the subdomain bypass that must not work
    assert _is_blocked_email("a@farm-d.example")             # listed as ".farm-d.example"


def test_walk_strips_whole_labels_off_the_front_only(ops):
    ops()
    assert not _is_blocked_email("a@notfarm-a.example")      # a string suffix, not a subdomain
    assert not _is_blocked_email("a@farm-a.example.org")     # the listed domain in the middle
    assert not _is_blocked_email("a@company.dev")


def test_a_bare_public_suffix_can_never_be_an_entry(ops):
    """`com` in the dashboard field must not refuse every address on earth."""
    ops("com, net, , @, .")
    assert get_settings().blocked_email_domain_set == frozenset()
    assert not _is_blocked_email("a@company.com")
    assert not _is_blocked_email("a@id")                      # the walk never tests the last label alone


def test_the_decision_logs_one_countable_line_per_block(ops, caplog):
    ops()
    with caplog.at_level(logging.WARNING, logger="treg.auth"):
        assert signup.blocked_email("Farm@Mail.Farm-A.example", "otp_start")
        assert not signup.blocked_email("ok@company.dev", "otp_start")
    lines = [r.getMessage() for r in caplog.records if "signup_blocked_domain" in r.getMessage()]
    assert lines == ["event=signup_blocked_domain door=otp_start domain=mail.farm-a.example"]


def test_the_decision_fails_open_on_a_classifier_error(monkeypatch, caplog):
    def boom(email):
        raise RuntimeError("bad blocklist")
    monkeypatch.setattr(signup, "_is_blocked_email", boom)
    with caplog.at_level(logging.ERROR, logger="treg.auth"):
        assert not signup.blocked_email("a@farm-a.example", "otp_start")   # the door stays open
    assert any("blocklist_error" in r.getMessage() for r in caplog.records)


# ---- the email OTP door --------------------------------------------------------------------------

async def test_otp_start_refuses_a_listed_domain_and_mints_no_code(client, ops):
    ops()
    for email in ("farm@farm-a.example", "farm@mail.farm-a.example", "Farm@FARM-B.EXAMPLE",
                  "farm@deep.farm-c.example", "farm@farm-d.example"):
        r = await _otp_start(client, email)
        assert r.status_code == 403, (email, r.text)
        assert r.json()["detail"] == REFUSAL
        assert "dev_code" not in r.json()                      # no code minted, nothing to verify


async def test_otp_refusal_names_no_list_and_no_domain(client, ops):
    ops()
    body = (await _otp_start(client, "farm@farm-a.example")).text.lower()
    for word in ("farm-a", "farm-b", "block", "list", "domain"):
        assert word not in body


async def test_otp_verify_refuses_a_code_minted_before_the_domain_was_listed(client, ops):
    email = "farm@farm-b.example"
    code = (await _otp_start(client, email)).json()["dev_code"]  # not yet listed: code issued
    ops()
    r = await client.post("/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code == 403 and r.json()["detail"] == REFUSAL
    assert "treg_session" not in r.headers.get("set-cookie", "")
    assert await _user_count(email) == 0                      # refused BEFORE the row is created


async def test_otp_refuses_sign_in_of_an_account_that_predates_the_listing(client, ops):
    """Sign-in, not only sign-up: an existing account on a listed domain gets no new session."""
    email = "early@farm-d.example"
    await _otp_login(client, email)
    assert await _user_count(email) == 1
    ops()
    assert (await _otp_start(client, email)).status_code == 403


async def test_otp_still_works_for_an_unlisted_domain_while_the_list_is_set(client, ops):
    ops()
    assert await _otp_login(client, "real@company.dev")
    assert await _otp_login(client, "farm-a.example@company.dev")   # local part is never looked at


# ---- open registration (POST /users: user + org + the $1 promo in one call) -----------------------

async def test_open_registration_refuses_a_blocked_domain_and_creates_nothing(client, ops):
    ops()
    for email in ("farm@sub.farm-a.example", "farm@farm-b.example"):
        r = await client.post("/users", json={"email": email})
        assert r.status_code == 403 and r.json()["detail"] == REFUSAL
        assert await _user_count(email) == 0
    async with session_maker() as s:
        assert (await s.execute(select(Org))).scalars().all() == []   # no team, so no grant


async def test_open_registration_is_unchanged_for_an_unlisted_domain(client, ops):
    ops("")
    r = await client.post("/users", json={"email": "someone@farm-a.example"})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "someone@farm-a.example"


# ---- creating a team (POST /orgs: the other promo door, for an already-registered identity) -------

async def test_create_org_refuses_an_identity_registered_before_its_domain_was_listed(client, ops):
    tok = await _otp_login(client, "early@farm-a.example")
    ops()
    r = await client.post("/orgs", json={"name": "Farm 1"}, headers={"X-Treg-Token": tok})
    assert r.status_code == 403 and r.json()["detail"] == REFUSAL
    async with session_maker() as s:
        assert (await s.execute(select(Org))).scalars().all() == []


# ---- the social doors (GitHub, Google) -----------------------------------------------------------

def _github_idp(email: str) -> FastAPI:
    g = FastAPI()

    @g.post("/login/oauth/access_token")
    async def token() -> dict:
        return {"access_token": "gho_test", "token_type": "bearer"}

    @g.get("/user")
    async def user() -> dict:
        return {"login": "farm", "email": email}

    return g


def _google_idp(email: str) -> FastAPI:
    g = FastAPI()

    @g.post("/token")
    async def token() -> dict:
        return {"access_token": "goog_test", "token_type": "bearer"}

    @g.get("/userinfo")
    async def userinfo() -> dict:
        return {"email": email, "email_verified": True}

    return g


@pytest.fixture
async def social(monkeypatch):
    """Both social doors configured against in-process fake identity providers. The blocklist goes
    in through the environment, as an operator would set it."""
    monkeypatch.setenv("TREG_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("TREG_GITHUB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("TREG_GITHUB_TOKEN_URL", "http://idp/login/oauth/access_token")
    monkeypatch.setenv("TREG_GITHUB_API_URL", "http://idp")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "gsec")
    monkeypatch.setenv("TREG_GOOGLE_TOKEN_URL", "http://idp/token")
    monkeypatch.setenv("TREG_GOOGLE_USERINFO_URL", "http://idp/userinfo")
    monkeypatch.setenv("TREG_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("TREG_BLOCKED_EMAIL_DOMAINS", OPS)
    get_settings.cache_clear()
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    if getattr(app.state, "http", None) is not None:
        await app.state.http.aclose()
    get_settings.cache_clear()


async def _social_callback(c: AsyncClient, door: str, idp: FastAPI):
    app.state.http = AsyncClient(transport=ASGITransport(app=idp), base_url="http://idp")
    r = await c.get(f"/auth/{door}", follow_redirects=False)
    assert r.status_code == 302
    state = c.cookies.get("treg_oauth_state")
    return await c.get(f"/auth/{door}/callback?code=abc&state={state}", follow_redirects=False)


@pytest.mark.parametrize("door,email", [
    ("github", "farm@farm-a.example"),          # a listed domain, exactly
    ("google", "farm@mail.farm-b.example"),     # a subdomain of a listed domain
])
async def test_social_login_on_a_blocked_domain_gets_a_refusal_page_and_no_session(social, door, email):
    idp = _github_idp(email) if door == "github" else _google_idp(email)
    cb = await _social_callback(social, door, idp)
    assert cb.status_code == 403, cb.text
    assert "cannot be used to sign in" in cb.text
    assert "farm-a" not in cb.text and "farm-b" not in cb.text
    assert "treg_session" not in cb.headers.get("set-cookie", "")
    assert (await social.get("/auth/me")).status_code == 401
    assert await _user_count(email) == 0


async def test_social_login_on_an_unlisted_domain_still_signs_in(social):
    cb = await _social_callback(social, "google", _google_idp("ok@company.dev"))
    assert cb.status_code == 302 and cb.headers["location"] == "/app"
    assert (await social.get("/auth/me")).json()["email"] == "ok@company.dev"


# ---- invites: the emailed link (mints a session) and the code (mints a membership token) ----------

@pytest.fixture
def sent_invites(monkeypatch):
    from treg import email as email_mod
    sent = []

    async def _capture(email, inviter, org_name, role, code, email_token, expires_at="", link_base="",
                       shared=""):
        sent.append({"email": email, "code": code, "email_token": email_token})
        return True

    monkeypatch.setattr(email_mod, "send_invite", _capture)
    return sent


async def _team_with_invite(c: AsyncClient, owner: str, invitee: str) -> dict:
    tok = await _otp_login(c, owner)
    org = (await c.post("/orgs", json={"name": "Real Team"}, headers={"X-Treg-Token": tok})).json()
    r = await c.post(f"/orgs/{org['org_id']}/invites", json={"email": invitee, "role": "member"},
                     headers={"X-Treg-Token": tok, "X-Treg-Org": org["org"]})
    assert r.status_code == 200, r.text
    return org


async def test_emailed_invite_link_refuses_a_blocked_domain(client, ops, sent_invites):
    await _team_with_invite(client, "owner@company.dev", "farm@farm-a.example")
    ops()
    t = sent_invites[0]["email_token"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as visitor:
        r = await visitor.post("/auth/invite-signin", content=f"t={t}",
                               headers={"content-type": "application/x-www-form-urlencoded"},
                               follow_redirects=False)
        assert r.status_code == 403 and "cannot be used to sign in" in r.text
        assert "treg_session" not in r.headers.get("set-cookie", "")
        assert (await visitor.get("/invites/mine")).status_code == 401
    assert await _user_count("farm@farm-a.example") == 0


async def test_invite_code_accept_refuses_a_blocked_domain(client, ops, sent_invites):
    await _team_with_invite(client, "owner@company.dev", "farm@sub.farm-b.example")
    ops()
    r = await client.post("/invites/accept",
                          json={"code": sent_invites[0]["code"], "email": "farm@sub.farm-b.example"})
    assert r.status_code == 403 and r.json()["detail"] == REFUSAL
    assert "token" not in r.json()
    assert await _user_count("farm@sub.farm-b.example") == 0
