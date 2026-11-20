"""A new account's first team, made for it (`first_run.first_team`, `POST /onboard/team`)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg.api import app
from treg.application.onboard import first_run
from treg.domain.identity import session as sess
from treg.infra.db import reset_db, session_maker
from treg.models import LedgerEntry, Membership, Org, User


def test_the_team_is_named_after_the_company_else_the_person():
    names = {e: first_run.team_names(e)[0] for e in (
        "ada@pixelforge.dev", "ada@mail.acme.com", "sam@acme.co.uk", "ada@my-cool-startup.ai",
        "john.doe@gmail.com", "ada@cs.stanford.edu", "1@qq.com", "a8f3k2x@outlook.com", "hi@gmail.com")}
    assert names == {
        "ada@pixelforge.dev": "Pixelforge", "ada@mail.acme.com": "Acme", "sam@acme.co.uk": "Acme",
        "ada@my-cool-startup.ai": "My Cool Startup",
        "john.doe@gmail.com": "John's team", "ada@cs.stanford.edu": "Ada's team",   # no company
        "1@qq.com": "My team", "a8f3k2x@outlook.com": "My team", "hi@gmail.com": "My team",  # no name
    }


@pytest.fixture
async def c():
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client


async def _new_user(email: str) -> int:
    async with session_maker() as db:
        u = User(email=email, email_verified_at=datetime.now(timezone.utc).replace(tzinfo=None))
        db.add(u)
        await db.commit()
        return u.id


async def test_a_new_account_gets_one_team_with_the_signup_credit(c):
    uid = await _new_user("ada@apollo.io")
    c.cookies.set("treg_session", sess.make_session(uid))
    # two tabs at once make one team between them, under the first name the registry accepts
    a, b = await asyncio.gather(c.post("/onboard/team"), c.post("/onboard/team"))
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
    made = [r.json() for r in (a, b) if r.json()["created"]]
    assert len(made) == 1 and made[0]["name"] == "Ada's team"    # "Apollo" is a catalog vendor's
    assert (await c.get("/auth/me")).json()["onboarded"] is True
    assert (await c.post("/onboard/team")).json() == {"created": False}
    async with session_maker() as db:
        assert len((await db.execute(select(Membership).where(Membership.user_id == uid))).scalars().all()) == 1
        grants = (await db.execute(select(LedgerEntry).where(LedgerEntry.org_id == made[0]["org_id"],
                                                             LedgerEntry.kind == "grant"))).scalars().all()
        assert len(grants) == 1                                    # the signup credit, as for a named team


async def test_an_account_in_a_team_it_named_gets_no_second_one(c):
    uid = await _new_user("old@x.io")
    c.cookies.set("treg_session", sess.make_session(uid))
    assert (await c.post("/orgs", json={"name": "Old team"})).status_code == 200
    assert (await c.post("/onboard/team")).json() == {"created": False}


async def test_the_legacy_personal_team_is_not_a_team_the_user_named(c):
    uid = await _new_user("cy@example.org")
    async with session_maker() as db:
        org = Org(name="cy@example.org", slug=f"cy-{uid}")
        db.add(org)
        await db.flush()
        db.add(Membership(user_id=uid, org_id=org.id, role="owner", token_hash=f"h{uid}"))
        await db.commit()
    c.cookies.set("treg_session", sess.make_session(uid))
    assert (await c.post("/onboard/team")).json()["name"] == "Example"


async def test_a_refused_first_team_hands_the_claim_back(c, monkeypatch):
    uid = await _new_user("ada@acme.io")
    c.cookies.set("treg_session", sess.make_session(uid))

    async def refuse(**_):
        raise first_run.signup.SignupError("blocked_domain")
    monkeypatch.setattr(first_run.signup, "create_org", refuse)
    assert (await c.post("/onboard/team")).status_code == 403
    assert (await c.get("/auth/me")).json()["onboarded"] is False   # a retry may still make it
    monkeypatch.undo()
    assert (await c.post("/onboard/team")).json()["name"] == "Acme"
