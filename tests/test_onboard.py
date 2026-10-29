"""Onboarding - /onboard/skip, and /onboard/reset for teams the retired onboarding demo created.

Those teams are real teams owned by the caller with fake, roster-only teammates. Reset removes them,
their data stays out of platform stats, and the fake-teammate domain can never sign in.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from conftest import make_upstream

from treg.api import app
from treg.infra.db import reset_db, session_maker
from treg.domain.identity import session as sess
from treg.models import DenyRule, Membership, Org, Tool, User

ADMIN = "ENV-ADMIN-SECRET"


def _h(t: str) -> dict:
    return {"X-Treg-Token": t}


@pytest.fixture
async def c(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    from treg.config import get_settings
    get_settings.cache_clear()
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()
    get_settings.cache_clear()


async def _register(c: AsyncClient, email: str) -> str:
    return (await c.post("/users", json={"email": email})).json()["token"]


async def _seed_demo_team(owner_email: str) -> str:
    """What the retired onboarding demo left behind: a demo team the caller owns, fake teammates and
    an echo tool."""
    async with session_maker() as s:
        owner = (await s.execute(select(User).where(User.email == owner_email))).scalar_one()
        org = Org(name="Acme Design", slug="acme-design", demo=True)
        s.add(org)
        await s.flush()
        s.add(Membership(user_id=owner.id, org_id=org.id, role="owner", token_hash=""))
        for handle, role in (("ada", "admin"), ("ben", "member")):
            mate = User(email=f"{handle}@demo.treg.local", demo=True, onboarded=True)
            s.add(mate)
            await s.flush()
            s.add(Membership(user_id=mate.id, org_id=org.id, role=role, token_hash=""))
        s.add(Tool(org_id=org.id, name="echo", owner=owner_email, base_url="https://postman-echo.com",
                   host="postman-echo.com", bindings=[]))
        await s.commit()
        return org.slug


async def test_skip_marks_onboarded_without_seeding(c):
    tok = await _register(c, "skip@x.io")
    assert (await c.post("/onboard/skip", headers=_h(tok))).json()["onboarded"] is True
    assert (await c.get("/auth/me", headers=_h(tok))).json()["onboarded"] is True
    async with session_maker() as s:
        assert (await s.execute(select(Org).where(Org.demo == True))).first() is None  # noqa: E712


async def test_reset_removes_demo_footprint(c):
    tok = await _register(c, "r@x.io")
    slug = await _seed_demo_team("r@x.io")
    rr = await c.post("/onboard/reset", headers=_h(tok))
    assert slug in rr.json()["removed"]
    async with session_maker() as s:
        assert (await s.execute(select(Org).where(Org.demo == True))).first() is None  # noqa: E712
        assert (await s.execute(select(User).where(User.demo == True))).first() is None  # noqa: E712


async def test_demo_teammate_cannot_request_a_login_code(c):
    r = await c.post("/auth/email/start", json={"email": "ada@demo.treg.local"})
    assert r.status_code == 400


async def test_demo_footprint_excluded_from_admin_stats(c):
    await _register(c, "real@x.io")  # 1 real user + a personal org
    before = (await c.get("/admin/stats", headers=_h(ADMIN))).json()["totals"]
    await _seed_demo_team("real@x.io")  # +2 demo users, +1 demo org, +1 tool
    after = (await c.get("/admin/stats", headers=_h(ADMIN))).json()["totals"]
    assert after["users"] == before["users"]  # the fake teammates don't count
    assert after["orgs"] == before["orgs"]     # the demo team doesn't count
    assert after["tools"] == before["tools"]   # nor its tool


async def test_reset_sweeps_deny_rules_naming_a_demo_teammate(c):
    """A member-scoped deny rule may name the demo teammate in the user's REAL team. `DenyRule.user_id`
    is a foreign key, so on Postgres deleting the demo User with that rule alive fails at the key
    and reset returns a 500. Every other path that deletes a User sweeps first; reset did not."""
    async with session_maker() as s:
        user = User(email="builder@x.io")
        s.add(user)
        await s.commit()
    tok = sess.make_identity(user.id)
    org = (await c.post("/orgs", json={"name": "Acme"}, headers=_h(tok))).json()
    oh = _h(org["token"])
    async with session_maker() as s:
        alex = User(email="alex@demo.treg.local", demo=True, onboarded=True)
        s.add(alex)
        await s.flush()
        s.add(Membership(user_id=alex.id, org_id=org["org_id"], role="member", token_hash=""))
        await s.commit()
    r = await c.post(f"/orgs/{org['org_id']}/deny", json={"host": "api.example.com", "user_id": alex.id}, headers=oh)
    assert r.status_code == 200, r.text
    rr = await c.post("/onboard/reset", headers=oh)
    assert rr.status_code == 200, rr.text
    async with session_maker() as s:
        assert await s.get(User, alex.id) is None
        assert (await s.execute(select(DenyRule).where(DenyRule.user_id == alex.id))).first() is None
