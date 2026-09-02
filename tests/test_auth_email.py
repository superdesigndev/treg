"""Email one-time-code login — the third identity door (alongside GitHub OAuth + per-org token).

Proving an email == login; first proof registers (creates a personal org). Dev mode returns the
code so dummy emails are testable without a mail sender.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings
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
    r = await c.post("/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def test_first_login_registers_user_with_first_team_then_reuses_identity(client):
    tok = await _otp_login(client, "neo@matrix.io")
    orgs = (await client.get("/orgs", headers={"X-Treg-Token": tok})).json()
    # Sign-in now creates the first team server-side (name guessed from the email domain) — the
    # welcome modal only renames it. See ensure_first_team for why this can't be a browser POST.
    assert [(o["name"], o["role"]) for o in orgs] == [("Matrix", "owner")]

    await _otp_login(client, "neo@matrix.io")  # second time = login, not a new user
    orgs2 = (await client.get("/orgs", headers={"X-Treg-Token": tok})).json()
    assert len(orgs2) == 1  # still no duplicate user; no second auto team either


async def test_verify_rejects_wrong_and_unknown_code(client):
    await client.post("/auth/email/start", json={"email": "trinity@matrix.io"})
    bad = await client.post("/auth/email/verify", json={"email": "trinity@matrix.io", "code": "000000"})
    assert bad.status_code == 401
    unknown = await client.post("/auth/email/verify", json={"email": "nobody@x.io", "code": "123456"})
    assert unknown.status_code == 401


async def test_code_is_one_time(client):
    email = "morpheus@matrix.io"
    code = (await client.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    ok = await client.post("/auth/email/verify", json={"email": email, "code": code})
    assert ok.status_code == 200
    replay = await client.post("/auth/email/verify", json={"email": email, "code": code})
    assert replay.status_code == 401  # consumed on first use


async def test_start_is_rate_limited_per_email(client):
    from treg.routers.auth import OTP_START_MAX_PER_EMAIL
    email = "flood@matrix.io"
    for _ in range(OTP_START_MAX_PER_EMAIL):
        assert (await client.post("/auth/email/start", json={"email": email})).status_code == 200
    blocked = await client.post("/auth/email/start", json={"email": email})
    assert blocked.status_code == 429  # the (N+1)th code request for one inbox is refused (email-bomb guard)


async def test_start_rate_limit_is_per_email_not_global(client):
    from treg.routers.auth import OTP_START_MAX_PER_EMAIL
    for _ in range(OTP_START_MAX_PER_EMAIL + 2):  # drive one inbox past its cap
        await client.post("/auth/email/start", json={"email": "victim@matrix.io"})
    other = await client.post("/auth/email/start", json={"email": "bystander@matrix.io"})
    assert other.status_code == 200  # a different inbox is unaffected (per-key window, not a global lock)


async def test_start_is_rate_limited_per_ip(client):
    from treg.routers.auth import OTP_START_MAX_PER_IP
    for i in range(OTP_START_MAX_PER_IP):  # distinct emails so the per-email cap never trips first
        assert (await client.post("/auth/email/start", json={"email": f"u{i}@matrix.io"})).status_code == 200
    blocked = await client.post("/auth/email/start", json={"email": "late@matrix.io"})
    assert blocked.status_code == 429  # a fresh inbox is blocked purely by the per-IP cap


async def test_dev_mode_off_hides_the_code(client):
    get_settings.cache_clear()
    settings = get_settings()
    object.__setattr__(settings, "email_dev_mode", False)
    try:
        r = (await client.post("/auth/email/start", json={"email": "a@b.io"})).json()
        assert r["sent"] is True and "dev_code" not in r
    finally:
        object.__setattr__(settings, "email_dev_mode", True)
        get_settings.cache_clear()


# ---- the first-team auto-create contract (ensure_first_team) --------------------------------


def _h(tok: str, org: str | None = None) -> dict:
    h = {"X-Treg-Token": tok}
    if org:
        h["X-Treg-Org"] = org
    return h


async def test_generic_email_domain_names_team_after_the_person(client):
    tok = await _otp_login(client, "sam@gmail.com")
    orgs = (await client.get("/orgs", headers={"X-Treg-Token": tok})).json()
    assert [o["name"] for o in orgs] == ["Sam"]  # "Gmail" helps nobody — use the local part


async def test_invited_user_gets_no_auto_team(client):
    # An invite is waiting for this email BEFORE their first login: they should JOIN that team,
    # not be handed a throwaway one next to it (the dashboard offers the join).
    owner = await _otp_login(client, "owner@acme.dev")
    org = (await client.post("/orgs", json={"name": "Acme"}, headers=_h(owner))).json()
    await client.post(f"/orgs/{org['org_id']}/invites", json={"email": "newhire@corp.com"},
                      headers=_h(owner, org["org"]))

    tok = await _otp_login(client, "newhire@corp.com")
    assert (await client.get("/orgs", headers=_h(tok))).json() == []
    mine = (await client.get("/invites/mine", headers=_h(tok))).json()
    assert [m["org"] for m in mine] == [org["org"]]


async def test_stranded_zero_team_user_selfheals_on_next_login(client):
    # A user registered before this feature (or whose auto-create failed) has zero teams; the
    # ensure runs on EVERY sign-in, so the next login makes their team.
    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import Membership, Org, User

    tok = await _otp_login(client, "stranded@oldco.io")
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == "stranded@oldco.io"))).scalar_one()
        ms = (await s.execute(select(Membership).where(Membership.user_id == u.id))).scalars().all()
        org = await s.get(Org, ms[0].org_id)
        await s.delete(ms[0])
        await s.delete(org)
        await s.commit()  # simulate the pre-feature stranded state: a user with no team at all
    assert (await client.get("/orgs", headers=_h(tok))).json() == []

    await _otp_login(client, "stranded@oldco.io")
    orgs = (await client.get("/orgs", headers=_h(tok))).json()
    assert [o["name"] for o in orgs] == ["Oldco"]


async def test_rename_org_changes_name_and_keeps_slug(client):
    tok = await _otp_login(client, "jax@jvullinghs.com")
    org = (await client.get("/orgs", headers=_h(tok))).json()[0]
    assert org["name"] == "Jvullinghs"

    r = await client.patch(f"/orgs/{org['org_id']}", json={"name": "Tibba"},
                           headers=_h(tok, org["slug"]))
    assert r.status_code == 200 and r.json() == {"org": org["slug"], "org_id": org["org_id"], "name": "Tibba"}

    after = (await client.get("/orgs", headers=_h(tok))).json()[0]
    assert after["name"] == "Tibba" and after["slug"] == org["slug"]  # slug (and tokens/URLs) stable

    blank = await client.patch(f"/orgs/{org['org_id']}", json={"name": "  "}, headers=_h(tok, org["slug"]))
    assert blank.status_code == 422


async def test_rename_org_requires_admin(client):
    owner = await _otp_login(client, "boss@tibba.co")
    org = (await client.get("/orgs", headers=_h(owner))).json()[0]
    await client.post(f"/orgs/{org['org_id']}/invites", json={"email": "viewer@corp.com", "role": "viewer"},
                      headers=_h(owner, org["slug"]))
    viewer = await _otp_login(client, "viewer@corp.com")
    inv = (await client.get("/invites/mine", headers=_h(viewer))).json()[0]
    await client.post(f"/invites/{inv['id']}/accept", headers=_h(viewer))

    r = await client.patch(f"/orgs/{org['org_id']}", json={"name": "Hijacked"},
                           headers=_h(viewer, org["slug"]))
    assert r.status_code == 403
