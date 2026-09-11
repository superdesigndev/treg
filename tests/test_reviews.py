"""Structured catalog reviews through the public HTTP boundary."""
import pytest
from sqlmodel import select

from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallRecord, CallReview, Feedback, LedgerEntry


async def seed(clients, **values):
    org = (await clients.post('/orgs', json={'name': 'Review source'})).json()
    async with session_maker() as db:
        db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev',
                          tool_name='example', method='GET', path='/',
                          **({'call_ref': 'review-call', 'status_code': 200,
                              'endpoint_id': 'example.search', 'provider': 'example'} | values)))
        await db.commit()
    return org


async def rows():
    async with session_maker() as db:
        return list((await db.execute(select(CallReview))).scalars())


async def submit(clients, org, **values):
    return await clients.post('/reviews', headers={'X-Treg-Token': org['token'],
                                                  'X-Treg-Client': 'review-test'},
                              json={'call_id': 'review-call', 'usefulness': 'useful'} | values)


async def test_review_receipt_retry_privacy_and_cleanup(clients):
    org = await seed(clients)
    response = await submit(clients, org, reason='  Helped the task.  ')
    assert response.status_code == 201, response.text
    retry = await submit(clients, org, usefulness='not_useful')
    assert retry.status_code == 200
    assert retry.json() == {'review_id': response.json()['review_id'], 'status': 'already_reviewed'}
    row, = await rows()
    assert (row.reason, row.usefulness, row.client, row.provider) == (
        'Helped the task.', 'useful', 'review-test', 'example')
    async with session_maker() as db:
        assert list((await db.execute(select(Feedback))).scalars()) == []
    assert (await clients.post('/reviews', json={'call_id': 'review-call',
                                                'usefulness': 'useful'})).status_code == 404
    assert (await clients.get('/admin/reviews')).status_code == 403
    slug = next(item['slug'] for item in (await clients.get('/orgs')).json()
                if item['org_id'] == org['org_id'])
    deleted = await clients.delete(f"/orgs/{org['org_id']}", params={'confirm': slug},
                                   headers={'X-Treg-Token': org['token']})
    assert deleted.status_code == 200, deleted.text
    assert await rows() == []


async def test_missing_and_ledger_only_are_retryable(clients):
    org = await seed(clients)
    async with session_maker() as db:
        db.add(LedgerEntry(id='review-ledger', org_id=org['org_id'], kind='settle',
                           amount_micro=0, call_id='ledger-only', endpoint_id='example.search'))
        await db.commit()
    for ref in ['unknown', 'ledger-only']:
        response = await submit(clients, org, call_id=ref)
        assert response.status_code == 404
        assert 'Retry shortly' in response.text
    assert await rows() == []


async def test_own_tool_cannot_be_reviewed(clients):
    org = await seed(clients, endpoint_id=None)
    assert (await submit(clients, org)).status_code == 400
    assert await rows() == []


@pytest.mark.parametrize('child', [True, False])
async def test_routed_attribution(clients, monkeypatch, child):
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    org = await seed(clients, endpoint_id='treg.search', provider='treg', credential_tier='routed')
    async with session_maker() as db:
        for ref, status, endpoint in [('review-call:r0', 503, 'failed.search'),
                                      ('review-call:r1', 200 if child else 500, 'winner.search'),
                                      ('reviewXcall:r2', 200, 'unrelated.search')]:
            db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev',
                              tool_name='example', method='GET', path='/', call_ref=ref,
                              status_code=status, endpoint_id=endpoint, provider=endpoint.split('.')[0]))
        await db.commit()
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    assert row.invited is False
    assert row.routed_via == 'treg.search'
    assert row.endpoint_id == ('winner.search' if child else 'treg.search')
    assert row.provider == ('winner' if child else 'treg')


@pytest.mark.parametrize('tier,rate,status,cached,invited', [
    ('platform', 1, 200, False, True), ('platform', 0, 200, False, False),
    ('platform', 1, 500, False, False), ('platform', 1, 200, True, False),
    ('credential', 1, 200, False, False), ('tool', 1, 200, False, False),
    ('routed', 1, 200, False, False), (None, 1, 200, False, False),
])
async def test_invited_recomputed(clients, monkeypatch, tier, rate, status, cached, invited):
    monkeypatch.setattr(get_settings(), 'review_sample_rate', rate)
    org = await seed(clients, credential_tier=tier, status_code=status, cached=cached)
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    assert row.invited is invited


@pytest.mark.parametrize('fields', [
    {'call_id': 'private@example.com'}, {'call_id': 'x' * 129}, {'call_id': ''},
    {'usefulness': 'excellent'}, {'reason': '  '}, {'reason': 'x' * 201},
    {'endpoint_id': 'spoofed'}, {'invited': True},
])
async def test_review_validation(clients, fields):
    response = await clients.post('/reviews', json={'call_id': 'id', 'usefulness': 'partly'} | fields)
    assert response.status_code == 422
    assert await rows() == []


async def test_admin_review_pagination(clients, monkeypatch):
    org = await seed(clients)
    await submit(clients, org)
    async with session_maker() as db:
        for ref, endpoint in [('second', 'example.search'), ('third', 'other.search')]:
            db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev',
                              tool_name='example', method='GET', path='/', status_code=200,
                              call_ref=ref, endpoint_id=endpoint))
        await db.commit()
    await submit(clients, org, call_id='second')
    await submit(clients, org, call_id='third')
    monkeypatch.setattr(get_settings(), 'admin_token', 'review-admin')
    headers = {'X-Treg-Token': 'review-admin'}
    first = (await clients.get('/admin/reviews', headers=headers,
                               params={'limit': 1, 'endpoint_id': 'example.search'})).json()
    second = (await clients.get('/admin/reviews', headers=headers, params={
        'limit': 1, 'endpoint_id': 'example.search', 'before': first['next_before'],
    })).json()
    assert first['items'][0]['id'] > second['items'][0]['id']
    assert second['items'][0]['endpoint_id'] == 'example.search'
    assert (await clients.get('/admin/reviews', headers=headers, params={'limit': 101})).status_code == 422


@pytest.mark.parametrize('surface', ['team', 'directory'])
async def test_mcp_review_relay(clients, surface):
    from test_mcp import _call_tool as team_call, mcp_session
    from test_mcp_directory import _call_tool as directory_call, directory_session

    org = await seed(clients)
    context = mcp_session(clients) if surface == 'team' else directory_session()
    call = team_call if surface == 'team' else directory_call
    async with context as client:
        result = await call(client, 'review', {'call_id': 'review-call', 'usefulness': 'partly',
                                             'reason': 'Some results helped.'}, token=org['token'])
    row, = await rows()
    assert result == {'review_id': row.id, 'status': 'received'}
    assert row.usefulness == 'partly'


async def test_mcp_review_schema():
    from treg.mcp import mcp, directory_mcp
    from treg.feedback_contract import REVIEW_DESCRIPTION, REVIEW_USEFULNESS

    for server in [mcp, directory_mcp]:
        tool = next(tool for tool in await server.list_tools() if tool.name == 'review')
        assert tool.description == REVIEW_DESCRIPTION
        assert tool.input_schema['properties']['usefulness']['enum'] == list(REVIEW_USEFULNESS)
        assert tool.input_schema['required'] == ['call_id', 'usefulness']
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False
        assert tool.annotations.idempotent_hint is False


async def test_commit_failure_never_persists_or_acknowledges_a_review(clients, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    org = await seed(clients)
    async def fail(self):
        raise RuntimeError('commit unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, 'commit', fail)
        with pytest.raises(RuntimeError, match='commit unavailable'):
            await submit(clients, org)
    assert await rows() == []
    assert (await submit(clients, org)).status_code == 201


async def test_concurrent_review_retries_share_one_receipt(clients):
    import asyncio

    org = await seed(clients)
    first, second = await asyncio.gather(submit(clients, org), submit(clients, org))
    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()['review_id'] == second.json()['review_id']
    assert len(await rows()) == 1


async def test_public_demo_cannot_submit_reviews(clients):
    from test_public_demo import _mint_public, _org_with_stripe_tool

    org_id = await _org_with_stripe_tool(clients)
    token = await _mint_public(clients, org_id)
    response = await clients.post('/reviews', headers={'X-Treg-Token': token},
                                  json={'call_id': 'id', 'usefulness': 'useful'})
    assert response.status_code == 403
    assert await rows() == []
