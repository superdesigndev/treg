"""Server execution is disabled before authentication, body parsing, or secret access."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from treg import audit, crypto
from treg.api import app
from treg.bootstrap import create_app
from treg.infra.db import get_session


@pytest.mark.parametrize("role, status", [("all", 503), ("control", 503), ("dataplane", 404)])
async def test_run_is_unavailable_on_every_deployment_role(role, status):
    async with AsyncClient(transport=ASGITransport(app=create_app(role)), base_url="http://registry") as client:
        response = await client.post("/run", json={"tool": "sh-skill", "args": ["-c", "echo hi"]})
    assert response.status_code == status
    if status == 503:
        assert response.json() == {"detail": "Server-side CLI execution is disabled"}


@pytest.mark.parametrize("identity", ["owner", "anonymous"])
async def test_run_disabled_without_dependencies_for_every_identity(clients, monkeypatch, identity):
    """No identity is resolved at all: the forbidden database below would catch any lookup, so an
    owner's token and no token stand for every role."""
    headers = dict(clients.headers)
    if identity == "anonymous":
        headers.pop("x-treg-token")
    await audit.drain()

    def forbidden(*args, **kwargs):
        pytest.fail("disabled /run accessed a database, secret, or subprocess dependency")

    async def forbidden_session():
        forbidden()

    with monkeypatch.context() as patch:
        patch.setitem(app.dependency_overrides, get_session, forbidden_session)
        patch.setattr(AsyncSession, "execute", forbidden)
        patch.setattr(crypto, "decrypt", forbidden)
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
