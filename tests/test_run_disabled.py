"""Server execution is disabled before authentication, body parsing, or secret access."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from treg import audit, crypto, runner
from treg.api import app
from treg.bootstrap import create_app
from treg.infra.db import get_session, session_maker
from treg.models import Membership, Org, User
from test_run import _register_runnable


async def test_run_disabled_for_registered_owner_and_cli_tool(clients):
    await _register_runnable(clients)
    response = await clients.post("/run", json={"tool": "sh-skill", "args": ["-c", "echo hi"]})
    assert response.status_code == 503
    assert response.json() == {"detail": "Server-side CLI execution is disabled"}
    await audit.drain()
    assert (await clients.get("/runs")).json() == []


@pytest.mark.parametrize("role, status", [("all", 503), ("control", 503), ("dataplane", 404)])
async def test_run_is_unavailable_on_every_deployment_role(role, status):
    async with AsyncClient(transport=ASGITransport(app=create_app(role)), base_url="http://registry") as client:
        response = await client.post("/run", json={"tool": "sh-skill", "args": ["-c", "echo hi"]})
    assert response.status_code == status
    if status == 503:
        assert response.json() == {"detail": "Server-side CLI execution is disabled"}


@pytest.mark.parametrize("identity", ["owner", "admin", "member", "viewer", "visitor", "sandbox", "anonymous"])
async def test_run_disabled_without_dependencies_for_every_identity(clients, monkeypatch, identity):
    headers = dict(clients.headers)
    if identity == "anonymous":
        headers.pop("x-treg-token")
    elif identity != "owner":
        token = crypto.new_token()
        async with session_maker() as db:
            org = (await db.execute(select(Org))).scalars().first()
            if identity == "sandbox":
                org.demo = True
                org.slug = "sbx-0123456789ab"
                db.add(org)
            email = ("visitor-test@sandbox.treg.local" if identity in {"visitor", "sandbox"}
                     else f"{identity}@example.invalid")
            user = User(email=email)
            db.add(user)
            await db.flush()
            db.add(Membership(user_id=user.id, org_id=org.id,
                              role="member" if identity in {"visitor", "sandbox"} else identity,
                              token_hash=crypto.hash_token(token)))
            await db.commit()
        headers["x-treg-token"] = token
    await audit.drain()

    def forbidden(*args, **kwargs):
        pytest.fail("disabled /run accessed a database, secret, or subprocess dependency")

    async def forbidden_session():
        forbidden()

    with monkeypatch.context() as patch:
        patch.setitem(app.dependency_overrides, get_session, forbidden_session)
        patch.setattr(AsyncSession, "execute", forbidden)
        patch.setattr(crypto, "decrypt", forbidden)
        patch.setattr(runner, "run_tool", forbidden)
        patch.setattr(asyncio, "create_subprocess_exec", forbidden)
        clients.headers.clear()
        clients.headers.update(headers)
        for path in ("/run", "/run/"):
            for body in (b'{"tool":"sh-skill","args":["-c","echo hi"]}', b"", b"{", b"null"):
                response = await clients.post(path, content=body, headers={"Content-Type": "application/json"},
                                              follow_redirects=True)
                assert response.status_code == 503
                assert response.json() == {"detail": "Server-side CLI execution is disabled"}
                assert len(response.history) == (1 if path.endswith("/") else 0)
