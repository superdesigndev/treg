"""The landing sandbox is closed: no new sandbox is minted, and an existing visitor token reaches
nothing. The reaper still deletes expired sandbox orgs and keeps a visitor that holds any other."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from conftest import drain_background_writes, make_upstream

from treg import sandbox
from treg.api import app
from treg.application.onboard import sandbox as onboard_sandbox
from treg.infra.db import reset_db, session_maker
from treg.models import Membership, Org, Secret, Tool, User


@pytest.fixture
async def anon():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://registry")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            yield c
    finally:
        await drain_background_writes()
        await app.state.http.aclose()


async def _mint() -> dict:
    async with session_maker() as db:
        return await onboard_sandbox.mint(db)


async def _expire_visitors() -> None:
    async with session_maker() as db:
        for u in (await db.execute(
                select(User).where(User.email.like(f"visitor-%@{sandbox.SANDBOX_DOMAIN}")))).scalars().all():
            u.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=sandbox.SANDBOX_TTL_MIN + 5)
        await db.commit()


async def test_mint_is_gone(anon):
    r = await anon.post("/demo/sandbox")
    assert r.status_code == 410
    async with session_maker() as db:
        assert not (await db.execute(select(Org))).scalars().all()


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/tools", None),
    ("POST", "/tools", {"name": "x", "base_url": "https://api.example.com"}),
    ("GET", "/call/https://api.stripe.com/v1/charges/", None),
    ("GET", "/demo/sandbox/live", None),
    ("GET", "/orgs", None),
    ("POST", "/orgs", {"name": "escaped"}),
    ("POST", "/onboard/demo", {"team_name": "Acme"}),
    ("POST", "/onboard/team", {}),
])
async def test_visitor_token_reaches_nothing(anon, method, path, body):
    tok = (await _mint())["token"]
    r = await anon.request(method, path, json=body, headers={"X-Treg-Token": tok})
    assert r.status_code == 403, r.text
    assert "sandbox is closed" in r.json()["detail"]


async def test_gc_reaps_expired_sandboxes(anon):
    await _mint()
    await _expire_visitors()
    async with session_maker() as db:
        assert await sandbox.gc(db) == 1
    async with session_maker() as db:
        for model in (Tool, Secret, Membership, Org, User):
            assert not (await db.execute(select(model))).scalars().all(), f"{model.__name__} rows survived gc"


async def test_gc_skips_a_sandbox_it_cannot_delete_and_reaps_the_rest(anon, monkeypatch):
    slugs = [(await _mint())["org_slug"] for _ in range(2)]
    poisoned = slugs[0]
    real_cascade = onboard_sandbox.cascade_delete_org

    async def cascade_unless_poisoned(org, db):
        if org.slug == poisoned:
            raise RuntimeError("this one will not delete")
        await real_cascade(org, db)

    monkeypatch.setattr(onboard_sandbox, "cascade_delete_org", cascade_unless_poisoned)
    await _expire_visitors()
    async with session_maker() as db:
        assert await sandbox.gc(db) == 1
    async with session_maker() as db:
        assert {o.slug for o in (await db.execute(select(Org))).scalars().all()} == {poisoned}
    monkeypatch.setattr(onboard_sandbox, "cascade_delete_org", real_cascade)
    async with session_maker() as db:
        assert await sandbox.gc(db) == 1


async def test_gc_keeps_a_visitor_that_belongs_to_a_real_team(anon):
    slugs = [(await _mint())["org_slug"] for _ in range(2)]
    await _expire_visitors()
    async with session_maker() as db:
        escaped = Org(name="Acme", slug="acme", demo=True)
        db.add(escaped)
        await db.flush()
        kept = (await db.execute(select(Membership).join(Org, Org.id == Membership.org_id)
                                 .where(Org.slug == slugs[0]))).scalar_one()
        db.add(Membership(user_id=kept.user_id, org_id=escaped.id, role="owner", token_hash=""))
        await db.commit()
        kept_user = kept.user_id
        assert await sandbox.gc(db) == 1
    async with session_maker() as db:
        assert {o.slug for o in (await db.execute(select(Org))).scalars().all()} == {slugs[0], "acme"}
        assert [u.id for u in (await db.execute(select(User))).scalars().all()] == [kept_user]
