"""The landing sandbox is gone, but a deployment may still hold its visitor users and their tokens.
Every identity resolver refuses them, so such a token reaches nothing."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

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


async def _leftover_visitor_token() -> str:
    """A visitor row as the sandbox left it: a member of its own demo team, holding a token."""
    token = crypto.new_token()
    async with session_maker() as db:
        user = User(email=f"visitor-0123456789ab@{SANDBOX_DOMAIN}", demo=True, onboarded=True)
        org = Org(name="Sandbox 0123456789ab", slug="sbx-0123456789ab", demo=True)
        db.add_all([user, org])
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=org.id, role="member", token_hash=crypto.hash_token(token)))
        await db.commit()
    return token


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/tools", None),
    ("POST", "/tools", {"name": "x", "base_url": "https://api.example.com"}),
    ("GET", "/call/https://api.stripe.com/v1/charges/", None),
    ("GET", "/orgs", None),
    ("POST", "/orgs", {"name": "escaped"}),
    ("POST", "/onboard/demo", {"team_name": "Acme"}),
    ("POST", "/onboard/team", {}),
])
async def test_a_leftover_visitor_token_reaches_nothing(anon, method, path, body):
    tok = await _leftover_visitor_token()
    r = await anon.request(method, path, json=body, headers={"X-Treg-Token": tok})
    assert r.status_code == 403, r.text
    assert "sandbox is closed" in r.json()["detail"]
