"""Browser identities receive one frontend across dashboard, catalog and shared URLs."""
import re

import pytest
from pydantic import ValidationError
from sqlmodel import select

from treg.config import Settings, get_settings
from treg.domain.identity import session
from treg.infra.db import session_maker
from treg.models import User
from treg.routers.web import _new_dashboard


async def identities():
    async with session_maker() as db:
        first = (await db.execute(select(User).where(User.email == 'tim@superdesign.dev'))).scalar_one()
        second = User(email='rollout-other@example.com')
        db.add(second)
        await db.commit()
        await db.refresh(second)
        return first, second


def assert_version(response, new):
    assert response.status_code == 200
    assert ('/app/ui/assets/' in response.text) is new
    assert ('/app/legacy/assets/' in response.text) is not new
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['vary'] == 'Cookie'


async def test_rollout_uses_verified_account_across_all_entrypoints(clients, monkeypatch):
    first, second = await identities()
    settings = get_settings()
    monkeypatch.setattr(settings, 'dashboard_rollout_enabled', True)
    monkeypatch.setattr(settings, 'dashboard_rollout_percent', 0)
    monkeypatch.setattr(settings, 'dashboard_rollout_user_ids', {first.id})
    paths = ['/app', '/app/tools/shared', '/app/skills/shared', '/catalog', '/catalog/google']
    for user, new in [(first, True), (second, False), (None, False)]:
        clients.cookies.clear()
        if user:
            clients.cookies.set(session.COOKIE, session.make_session(user.id, token_version=user.token_version))
        for path in paths:
            assert_version(await clients.get(path), new)
    # Neither a query override nor a forged session chooses a frontend.
    clients.cookies.set(session.COOKIE, 'forged')
    assert_version(await clients.get('/app?dashboard=new'), False)
    clients.cookies.set(session.COOKIE, session.make_session(first.id, token_version=first.token_version))
    monkeypatch.setattr(settings, 'dashboard_rollout_enabled', False)
    for path in paths:
        assert_version(await clients.get(path), False)


async def test_legacy_assets_are_isolated_and_served(clients):
    response = await clients.get('/app')
    assert_version(response, False)
    assets = re.findall(r'<script src="(/app/legacy/assets/[^"\s]+)"', response.text)
    assert assets
    # Runtime-configured analytics/ad routes must not become raw frozen JavaScript.
    assert '<script src="/sitetrack.js"></script>' in response.text
    assert '<script src="/adtrack.js"></script>' in response.text
    tracking = await clients.get('/sitetrack.js')
    assert '{POSTHOG_KEY}' not in tracking.text
    for asset in assets:
        loaded = await clients.get(asset)
        assert loaded.status_code == 200
        assert 'javascript' in loaded.headers['content-type']
        assert 'immutable' in loaded.headers['cache-control']
    for path in ['missing.js', '%2e%2e/index.html']:
        assert (await clients.get('/app/legacy/assets/' + path)).status_code == 404


def test_rollout_buckets_are_stable_monotonic_and_account_based(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'dashboard_rollout_enabled', True)
    monkeypatch.setattr(settings, 'dashboard_rollout_user_ids', set())
    users = [User(id=i, email=f'{i}@example.com') for i in range(1, 501)]
    cohorts = []
    for percent in [0, 10, 50, 100]:
        monkeypatch.setattr(settings, 'dashboard_rollout_percent', percent)
        cohort = {u.id for u in users if _new_dashboard(u)}
        assert cohort == {u.id for u in users if _new_dashboard(User(id=u.id, email='changed@example.com'))}
        cohorts.append(cohort)
    assert cohorts[0] == set()
    assert 0 < len(cohorts[1]) < len(cohorts[2]) < len(cohorts[3]) == 500
    assert cohorts[1] < cohorts[2] < cohorts[3]


async def test_anonymous_visitors_follow_only_the_full_rollout(clients, monkeypatch):
    """No account means no bucket: signed-out entries move to the new frontend at 100% only, and
    move back when the percentage drops or the rollout is switched off."""
    settings = get_settings()
    paths = ['/app', '/app/tools/shared', '/app/skills/shared', '/catalog', '/catalog/google']
    for enabled, percent, new in [(True, 99, False), (True, 100, True), (False, 100, False)]:
        monkeypatch.setattr(settings, 'dashboard_rollout_enabled', enabled)
        monkeypatch.setattr(settings, 'dashboard_rollout_percent', percent)
        assert _new_dashboard(None) is new
        clients.cookies.clear()
        for path in paths:
            assert_version(await clients.get(path), new)


@pytest.mark.parametrize('value', [-1, 101])
def test_rollout_rejects_invalid_percentage(value):
    with pytest.raises(ValidationError):
        Settings(dashboard_rollout_percent=value)


async def test_revoked_session_cannot_enter_allowlisted_frontend(clients, monkeypatch):
    first, _ = await identities()
    settings = get_settings()
    monkeypatch.setattr(settings, 'dashboard_rollout_enabled', True)
    monkeypatch.setattr(settings, 'dashboard_rollout_user_ids', {first.id})
    clients.cookies.set(session.COOKIE, session.make_session(first.id, token_version=first.token_version))
    async with session_maker() as db:
        user = await db.get(User, first.id)
        user.token_version += 1
        await db.commit()
    assert_version(await clients.get('/app'), False)


def test_rollout_policy_changes_refresh_stamp(monkeypatch):
    from treg.api import _app_version
    settings = get_settings()
    before = _app_version()
    monkeypatch.setattr(settings, 'dashboard_rollout_enabled', not settings.dashboard_rollout_enabled)
    assert _app_version() != before


async def test_signed_in_entries_record_the_served_frontend(clients, monkeypatch, posthog_events):
    first, second = await identities()
    settings = get_settings()
    monkeypatch.setattr(settings, 'dashboard_rollout_enabled', True)
    monkeypatch.setattr(settings, 'dashboard_rollout_percent', 0)
    monkeypatch.setattr(settings, 'dashboard_rollout_user_ids', {first.id})
    for user in (first, second):
        clients.cookies.set(session.COOKIE, session.make_session(user.id, token_version=user.token_version))
        await clients.get('/app')
    clients.cookies.clear()
    await clients.get('/app')
    events = await posthog_events('dashboard_served')
    assert [(e['distinct_id'], e['properties']['variant'], e['properties']['assignment']) for e in events] == [
        (first.email, 'new', 'allowlist'), (second.email, 'legacy', 'bucket')]
    assert events[1]['properties']['$set'] == {
        'dashboard_variant': 'legacy', 'dashboard_bucket': events[1]['properties']['bucket']}
    assert events[1]['properties']['rollout_percent'] == 0
