"""The redesigned dashboard must receive real local assets, not an HTML fallback."""

import pytest


@pytest.fixture
async def new_dashboard(clients):
    from treg.infra.db import session_maker
    from treg.models import User
    from treg.domain.identity import session
    from sqlmodel import select
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == 'tim@superdesign.dev'))).scalar_one()
        clients.cookies.set(session.COOKIE, session.make_session(user.id, token_version=user.token_version))
    return clients


async def test_dashboard_dev_entry_refuses_a_public_hostname(new_dashboard, monkeypatch):
    clients = new_dashboard
    import pytest
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), 'frontend_dev', True)
    monkeypatch.setattr(get_settings(), 'public_url', 'https://registry.example.com')
    with pytest.raises(RuntimeError, match='loopback'):
        await clients.get('/app')

