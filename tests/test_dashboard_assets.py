"""The redesigned dashboard must receive real local assets, not an HTML fallback."""

from pathlib import Path
import re
import pytest


from treg import api


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


async def test_every_entry_serves_the_compiled_app_to_every_visitor(new_dashboard):
    """Signed in, signed out or with a forged session, every Dashboard entry is the one compiled app,
    never cached across visitors."""
    clients = new_dashboard
    paths = ['/app', '/app/tools/shared', '/app/skills/shared', '/catalog', '/catalog/google', '/search']
    signed_in = dict(clients.cookies)
    for cookies in (signed_in, {}, {'treg_session': 'forged'}):
        clients.cookies.clear()
        clients.cookies.update(cookies)
        for path in paths:
            page = await clients.get(path)
            assert page.status_code == 200, path
            assert '/app/ui/assets/' in page.text, path
            assert page.headers['cache-control'] == 'private, no-store', path
            assert page.headers['vary'] == 'Cookie', path


async def test_dashboard_redesign_assets_are_served_from_the_same_origin(new_dashboard):
    clients = new_dashboard
    page = await clients.get('/app')
    assert page.status_code == 200
    paths = set(re.findall(r'(?:href|src)="(/media/redesign/[^"\']+)"', page.text))
    assets = set(re.findall(r'(?:href|src)="(/app/ui/assets/[^"\']+)"', page.text))
    assert any(p.endswith('.js') for p in assets)
    assert any(p.endswith('.css') for p in assets)
    assert page.headers['cache-control'] == 'private, no-store'
    for asset in assets:
        response = await clients.get(asset)
        assert response.status_code == 200
        assert 'immutable' in response.headers['cache-control']
        assert not response.headers['content-type'].startswith('text/html')
    missing = await clients.get('/app/ui/assets/missing.js')
    assert missing.status_code == 404
    assert '<!doctype' not in missing.text.lower()
    assert (await clients.get('/app/ui/assets/%2e%2e/index.html')).status_code == 404
    # Include the dynamic task, provider and token-state images as well as literal URLs.
    asset_dir = Path(api.__file__).parent / 'web' / 'media' / 'redesign'
    paths.update('/media/redesign/' + p.name for p in asset_dir.iterdir() if p.suffix in {'.svg', '.png', '.jpg'})
    for path in sorted(paths):
        response = await clients.get(path)
        assert response.status_code == 200, path
        assert response.content, path
        expected_type = 'text/css' if path.endswith('.css') else 'image/'
        assert response.headers['content-type'].startswith(expected_type), path


async def test_dashboard_dev_entry_refuses_a_public_hostname(new_dashboard, monkeypatch):
    clients = new_dashboard
    import pytest
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), 'frontend_dev', True)
    monkeypatch.setattr(get_settings(), 'public_url', 'https://registry.example.com')
    with pytest.raises(RuntimeError, match='loopback'):
        await clients.get('/app')
