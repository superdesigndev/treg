"""Call matrix fixtures wired through the real relay and an in-process provider."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app

from test_marketplace_call import platform_on  # noqa: F401 — fixture reuse, one roster for both

from .provider import FakeProvider
from .transport import FaultTransport


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
async def matrix_clients(clients: AsyncClient, fake_provider: FakeProvider):
    previous = app.state.http
    upstream = AsyncClient(
        transport=FaultTransport(ASGITransport(app=fake_provider.app)),
        base_url="https://fake-provider.invalid",
    )
    app.state.http = upstream
    try:
        yield clients
    finally:
        app.state.http = previous
        await upstream.aclose()
