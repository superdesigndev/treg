"""Call marks through the public HTTP boundary."""
import asyncio

import pytest
from sqlmodel import select

from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallMark, CallRecord, CallReview, Feedback

WRONG = [{'index': 2, 'field': 'email', 'verdict': 'wrong_entity'}]


async def seed(clients, *refs, **values):
    org = (await clients.post('/orgs', json={'name': 'Mark source'})).json()
    async with session_maker() as db:
        for ref in refs or ('mark-call',):
            db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev',
                              tool_name='example', method='GET', path='/',
                              **({'call_ref': ref, 'status_code': 200,
                                  'endpoint_id': 'example.search', 'provider': 'example'} | values)))
        await db.commit()
    return org


async def rows():
    async with session_maker() as db:
        return list((await db.execute(select(CallMark).order_by(CallMark.id))).scalars())


async def submit(clients, org, **values):
    return await clients.post('/marks', headers={'X-Treg-Token': org['token'],
                                                'X-Treg-Client': 'mark-test'},
                              json={'call_id': 'mark-call', 'basis': 'bounced', 'items': WRONG} | values)


async def test_mark_receipt_retry_append_and_cleanup(clients):
    org = await seed(clients)
    response = await submit(clients, org, checked=10, note='  Bounced on send.  ')
    assert response.status_code == 201, response.text
    retry = await submit(clients, org, checked=10, note='Bounced on send.')
    assert retry.status_code == 200
    assert retry.json() == {'mark_id': response.json()['mark_id'], 'status': 'already_received'}
    # a second finding on the same call is appended, never folded into the first
    later = await submit(clients, org, items=[{'index': 6, 'verdict': 'stale'}], basis='cross_checked')
    assert later.status_code == 201
    first, second = await rows()
    assert (first.items, first.checked, first.note, first.client, first.endpoint_id, first.provider) == (
        WRONG, 10, 'Bounced on send.', 'mark-test', 'example.search', 'example')
    assert second.items == [{'index': 6, 'verdict': 'stale'}]
    async with session_maker() as db:
        assert list((await db.execute(select(Feedback))).scalars()) == []
        assert list((await db.execute(select(CallReview))).scalars()) == []
    assert (await clients.post('/marks', json={'call_id': 'mark-call', 'basis': 'bounced',
                                              'items': WRONG})).status_code == 404
    assert (await clients.get('/admin/marks')).status_code == 403
    slug = next(item['slug'] for item in (await clients.get('/orgs')).json()
                if item['org_id'] == org['org_id'])
    deleted = await clients.delete(f"/orgs/{org['org_id']}", params={'confirm': slug},
                                   headers={'X-Treg-Token': org['token']})
    assert deleted.status_code == 200, deleted.text
    assert await rows() == []


async def test_replace_and_withdraw_stay_on_one_call_and_team(clients):
    org = await seed(clients, 'mark-call', 'other-call')
    first = (await submit(clients, org)).json()['mark_id']
    corrected = await submit(clients, org, replaces=first,
                             items=[{'index': 2, 'field': 'email', 'verdict': 'correct'}])
    assert corrected.status_code == 201
    withdrawn = await submit(clients, org, replaces=corrected.json()['mark_id'], items=[])
    assert withdrawn.status_code == 201
    assert [row.replaces for row in await rows()] == [None, first, corrected.json()['mark_id']]
    assert (await submit(clients, org, call_id='other-call', replaces=first)).status_code == 404
    stranger = await seed(clients)
    assert (await submit(clients, stranger, replaces=first)).status_code == 404
    assert (await submit(clients, org, replaces=99999)).status_code == 404


async def test_evidence_is_verified_only_inside_the_team(clients):
    org = await seed(clients, 'mark-call', 'verify-call')
    await seed(clients, 'their-call')
    assert (await submit(clients, org, evidence_call_id='verify-call')).status_code == 201
    assert (await submit(clients, org, evidence_call_id='their-call')).status_code == 201
    assert [(row.evidence_call_id, row.evidence_verified) for row in await rows()] == [
        ('verify-call', True), ('their-call', False)]


async def test_missing_call_is_retryable_and_own_tool_refused(clients):
    org = await seed(clients)
    response = await submit(clients, org, call_id='unknown')
    assert response.status_code == 404
    assert 'Retry shortly' in response.text
    own = await seed(clients, endpoint_id=None)
    assert (await submit(clients, own)).status_code == 400
    assert await rows() == []


async def test_routed_mark_keeps_the_called_endpoint(clients):
    org = await seed(clients, endpoint_id='treg.search', provider='treg', credential_tier='routed')
    async with session_maker() as db:
        db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev', tool_name='example',
                          method='GET', path='/', call_ref='mark-call:r0', status_code=200,
                          endpoint_id='winner.search', provider='winner'))
        await db.commit()
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    # row positions are the merged answer's, so no child is credited or blamed here
    assert (row.endpoint_id, row.provider, row.credential_tier) == ('treg.search', 'treg', 'routed')


async def test_rate_limit(clients, monkeypatch):
    from treg.application import feedback as feedback_app

    monkeypatch.setattr(feedback_app, 'MARK_RATE_MAX', 2)
    org = await seed(clients)
    for index in range(2):
        assert (await submit(clients, org, items=[{'index': index, 'verdict': 'wrong'}])).status_code == 201
    assert (await submit(clients, org, items=[{'index': 9, 'verdict': 'wrong'}])).status_code == 429
    # an identical retry of an accepted mark is still answered
    assert (await submit(clients, org, items=[{'index': 0, 'verdict': 'wrong'}])).status_code == 200
    assert len(await rows()) == 2


@pytest.mark.parametrize('fields', [
    {'call_id': 'private@example.com'}, {'basis': 'guess'}, {'items': []},
    {'items': [{'verdict': 'excellent'}]}, {'items': [{'verdict': 'wrong', 'index': -1}]},
    {'items': [{'verdict': 'wrong', 'field': 'a b'}]}, {'items': [{'verdict': 'wrong', 'x': 1}]},
    {'items': [{'verdict': 'wrong'}] * 201}, {'checked': -1}, {'note': ' '}, {'note': 'x' * 501},
    {'replaces': 0}, {'endpoint_id': 'spoofed'}, {'evidence_call_id': 'a@b'},
])
async def test_mark_validation(clients, fields):
    response = await clients.post('/marks', json={'call_id': 'id', 'basis': 'bounced',
                                                 'items': WRONG} | fields)
    assert response.status_code == 422
    assert await rows() == []


async def test_admin_mark_pagination(clients, monkeypatch):
    org = await seed(clients, 'mark-call', 'second')
    await submit(clients, org)
    await submit(clients, org, call_id='second')
    monkeypatch.setattr(get_settings(), 'admin_token', 'mark-admin')
    headers = {'X-Treg-Token': 'mark-admin'}
    first = (await clients.get('/admin/marks', headers=headers, params={'limit': 1})).json()
    second = (await clients.get('/admin/marks', headers=headers,
                                params={'limit': 1, 'before': first['next_before']})).json()
    assert first['items'][0]['id'] > second['items'][0]['id']
    assert first['items'][0]['items'] == WRONG


async def test_concurrent_identical_marks_share_one_receipt(clients):
    org = await seed(clients)
    first, second = await asyncio.gather(submit(clients, org), submit(clients, org))
    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()['mark_id'] == second.json()['mark_id']
    assert len(await rows()) == 1


async def test_public_demo_cannot_submit_marks(clients):
    from test_public_demo import _mint_public, _org_with_stripe_tool

    org_id = await _org_with_stripe_tool(clients)
    token = await _mint_public(clients, org_id)
    response = await clients.post('/marks', headers={'X-Treg-Token': token},
                                  json={'call_id': 'id', 'basis': 'bounced', 'items': WRONG})
    assert response.status_code == 403
    assert await rows() == []


async def test_mcp_mark_relay(clients):
    from test_mcp import _call_tool as team_call, mcp_session

    org = await seed(clients)
    async with mcp_session(clients) as client:
        result = await team_call(client, 'mark', {'call_id': 'mark-call', 'basis': 'user_said',
                                                  'items': [{'verdict': 'stale', 'index': 1}],
                                                  'checked': 3}, token=org['token'])
    row, = await rows()
    assert result == {'mark_id': row.id, 'status': 'received'}
    assert (row.basis, row.items, row.checked, row.note) == (
        'user_said', [{'index': 1, 'verdict': 'stale'}], 3, None)


@pytest.mark.parametrize('item', [{'verdict': 'wrong', 'row': 2}, {'verdict': 'wrong', 'index': -1},
                                  {'verdict': 'wrong', 'field': 'a b'}])
async def test_mcp_refuses_an_item_it_cannot_place(clients, item):
    """A misspelled or invalid locator is an error, never a finding about the whole result."""
    from test_mcp import _rpc, mcp_session

    org = await seed(clients)
    async with mcp_session(clients) as client:
        await _rpc(client, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                          'clientInfo': {'name': 'test', 'version': '1'}}, org['token'])
        response = await _rpc(client, 'tools/call', {'name': 'mark', 'arguments': {
            'call_id': 'mark-call', 'basis': 'bounced', 'items': [item]}}, org['token'])
    result = response.json().get('result') or {}
    assert result.get('isError') is True or 'error' in response.json(), response.text
    assert await rows() == []
