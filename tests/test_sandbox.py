"""The landing sandbox is closed: no new sandbox is minted, and an existing visitor token reaches
nothing."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from conftest import drain_background_writes, make_upstream

from treg import crypto
from treg.api import app
from treg.domain.identity.access import SANDBOX_DOMAIN
from treg.infra.db import reset_db, session_maker
from treg.models import Membership, Org, User


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


async def _visitor_token() -> str:
    """A visitor left over from before the sandbox closed: a demo `sbx-` team and its member token."""
    token = crypto.new_token()
    async with session_maker() as db:
        org = Org(name="Sandbox", slug="sbx-0123456789ab", demo=True)
        user = User(email=f"visitor-0123456789ab@{SANDBOX_DOMAIN}")
        db.add_all([org, user])
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=org.id, role="member", token_hash=crypto.hash_token(token)))
        await db.commit()
    return token


async def test_mint_is_gone(anon):
    r = await anon.post("/demo/sandbox")
    assert r.status_code == 410
    async with session_maker() as db:
        assert not (await db.execute(select(Org))).scalars().all()


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/tools", None),
    ("POST", "/tools", {"name": "x", "base_url": "https://api.example.com"}),
    ("GET", "/call/https://api.stripe.com/v1/charges/", None),
    ("GET", "/orgs", None),
    ("POST", "/orgs", {"name": "escaped"}),
    ("POST", "/onboard/team", {}),
])
async def test_visitor_token_reaches_nothing(anon, method, path, body):
    tok = await _visitor_token()
    r = await anon.request(method, path, json=body, headers={"X-Treg-Token": tok})
    assert r.status_code == 403, r.text
    assert "sandbox is closed" in r.json()["detail"]
