"""Owned-team limits through the same HTTP routes used by the dashboard and CLI."""
import asyncio


async def test_eleventh_owned_team_is_refused(clients):
    # The authenticated fixture already owns its first team.
    for i in range(9):
        r = await clients.post('/orgs', json={'name': f'team-{i}'})
        assert r.status_code == 200, r.text
    before = (await clients.get('/orgs')).json()
    r = await clients.post('/orgs', json={'name': 'eleventh'})
    assert r.status_code == 403, r.text
    assert '10 teams' in r.json()['detail']
    assert (await clients.get('/orgs')).json() == before


async def test_concurrent_creations_share_the_last_slot(clients):
    for i in range(8):
        assert (await clients.post('/orgs', json={'name': f'team-{i}'})).status_code == 200
    responses = await asyncio.gather(*(
        clients.post('/orgs', json={'name': f'racing-{i}'}) for i in range(4)
    ))
    assert sorted(r.status_code for r in responses) == [200, 403, 403, 403]
    assert len((await clients.get('/orgs')).json()) == 10


async def test_deletion_frees_a_slot_and_legacy_first_team_counts(clients):
    first = (await clients.post('/users', json={'email': 'legacy-limit@example.org'})).json()
    headers = {'X-Treg-Token': first['token']}
    for i in range(9):
        r = await clients.post('/orgs', headers=headers, json={'name': f'legacy-{i}'})
        assert r.status_code == 200
    last = r.json()
    assert (await clients.post('/orgs', headers=headers, json={'name': 'blocked'})).status_code == 403
    deleted = await clients.delete(f"/orgs/{last['org_id']}", params={'confirm': last['org']},
                                   headers={'X-Treg-Token': last['token']})
    assert deleted.status_code == 200, deleted.text
    assert (await clients.post('/orgs', headers=headers, json={'name': 'replacement'})).status_code == 200
    assert (await clients.post('/orgs', headers=headers, json={'name': 'blocked-again'})).status_code == 403


async def test_joining_does_not_count_but_promotion_does(clients):
    from sqlmodel import select
    from treg.infra.db import session_maker
    from treg.models import User

    other = (await clients.post('/users', json={'email': 'other-owner@example.org'})).json()
    other_headers = {'X-Treg-Token': other['token']}
    inv = await clients.post(f"/orgs/{other['org_id']}/invites", headers=other_headers,
                             json={'email': 'tim@superdesign.dev', 'role': 'member'})
    accepted = await clients.post('/invites/accept', json={
        'code': inv.json()['code'], 'email': 'tim@superdesign.dev'})
    assert accepted.status_code == 200, accepted.text
    for i in range(9):
        assert (await clients.post('/orgs', json={'name': f'owned-{i}'})).status_code == 200
    assert len((await clients.get('/orgs')).json()) == 11
    async with session_maker() as db:
        uid = (await db.execute(select(User.id).where(User.email == 'tim@superdesign.dev'))).scalar_one()
    promoted = await clients.patch(f"/orgs/{other['org_id']}/members/{uid}",
                                   headers=other_headers, json={'role': 'owner'})
    assert promoted.status_code == 403, promoted.text
    assert (await clients.get('/tools', headers={'X-Treg-Token': accepted.json()['token']})).status_code == 200


async def test_demo_cannot_bypass_limit_but_existing_demo_can_be_reused(clients):
    for i in range(8):
        assert (await clients.post('/orgs', json={'name': f'owned-{i}'})).status_code == 200
    demo = await clients.post('/onboard/demo', json={'team_name': 'Demo'})
    assert demo.status_code == 200, demo.text
    assert (await clients.post('/orgs', json={'name': 'eleventh'})).status_code == 403
    reused = await clients.post('/onboard/demo', json={'team_name': 'Demo again'})
    assert reused.status_code == 200, reused.text
    assert len((await clients.get('/orgs')).json()) == 10


async def test_demo_refused_at_ten_and_existing_excess_teams_preserved(clients):
    from sqlmodel import select
    from treg.infra.db import session_maker
    from treg.models import Membership, Org, User

    # Seed the pre-upgrade state, which normal creation can no longer produce.
    async with session_maker() as db:
        uid = (await db.execute(select(User.id).where(User.email == 'tim@superdesign.dev'))).scalar_one()
        for i in range(11):
            org = Org(name=f'Existing {i}', slug=f'existing-{i}')
            db.add(org)
            await db.flush()
            db.add(Membership(user_id=uid, org_id=org.id, role='owner', token_hash=f'old-{i}'))
        await db.commit()
    before = (await clients.get('/orgs')).json()
    assert len(before) == 12
    assert (await clients.post('/orgs', json={'name': 'new'})).status_code == 403
    assert (await clients.post('/onboard/demo', json={'team_name': 'demo'})).status_code == 403
    assert (await clients.get('/orgs')).json() == before
    assert (await clients.get('/tools')).status_code == 200


async def test_promotion_and_creation_compete_for_same_slot(clients):
    from sqlmodel import select
    from treg.infra.db import session_maker
    from treg.models import User

    other = (await clients.post('/users', json={'email': 'race-owner@example.org'})).json()
    headers = {'X-Treg-Token': other['token']}
    inv = await clients.post(f"/orgs/{other['org_id']}/invites", headers=headers,
                             json={'email': 'tim@superdesign.dev'})
    assert (await clients.post('/invites/accept', json={
        'code': inv.json()['code'], 'email': 'tim@superdesign.dev'})).status_code == 200
    for i in range(8):
        assert (await clients.post('/orgs', json={'name': f'before-race-{i}'})).status_code == 200
    async with session_maker() as db:
        uid = (await db.execute(select(User.id).where(User.email == 'tim@superdesign.dev'))).scalar_one()
    results = await asyncio.gather(
        clients.post('/orgs', json={'name': 'last-slot'}),
        clients.patch(f"/orgs/{other['org_id']}/members/{uid}", headers=headers, json={'role': 'owner'}),
    )
    assert sorted(r.status_code for r in results) == [200, 403]
    assert sum(t['role'] == 'owner' for t in (await clients.get('/orgs')).json()) == 10
