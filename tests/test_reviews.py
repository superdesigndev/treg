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


@pytest.mark.parametrize(('header', 'client'), [('Claude-Code/2.1.0', 'claude-code'), ('mcp-prior', 'mcp-prior')])
async def test_review_normalizes_client_attribution(clients, header, client):
    org = await seed(clients)
    response = await clients.post('/reviews', headers={'X-Treg-Token': org['token'], 'X-Treg-Client': header},
                                  json={'call_id': 'review-call', 'usefulness': 'useful'})
    assert response.status_code == 201, response.text
    row, = await rows()
    assert row.client == client


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
                                      # an async attempt's poll: the job endpoint, not what was asked
                                      ('review-call:r1:p1', 200, 'winner.jobs.get'),
                                      ('reviewXcall:r2', 200, 'unrelated.search')]:
            db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev',
                              tool_name='example', method='GET', path='/', call_ref=ref,
                              status_code=status, endpoint_id=endpoint, provider=endpoint.split('.')[0]))
        await db.commit()
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    assert row.invited is True  # routed calls are invited, recomputed from the parent's tier
    assert row.routed_via == 'treg.search'
    assert row.endpoint_id == ('winner.search' if child else 'treg.search')
    assert row.provider == ('winner' if child else 'treg')


@pytest.mark.parametrize('tier,rate,status,cached,invited', [
    ('platform', 1, 200, False, True), ('platform', 0, 200, False, False),
    ('platform', 1, 500, False, False), ('platform', 1, 200, True, False),
    ('credential', 1, 200, False, False), ('tool', 1, 200, False, False),
    ('routed', 1, 200, False, True), ('routed', 0, 200, False, False), (None, 1, 200, False, False),
])
async def test_invited_recomputed(clients, monkeypatch, tier, rate, status, cached, invited):
    monkeypatch.setattr(get_settings(), 'review_sample_rate', rate)
    org = await seed(clients, credential_tier=tier, status_code=status, cached=cached)
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    assert row.invited is invited


@pytest.mark.parametrize('fields', [
    {'call_id': 'private@example.com'}, {'call_id': 'x' * 129},
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


async def test_mcp_review_relay(clients):
    from test_mcp import _call_tool as team_call, mcp_session

    org = await seed(clients)
    async with mcp_session(clients) as client:
        result = await team_call(client, 'review', {'call_id': 'review-call', 'usefulness': 'partly',
                                             'reason': 'Some results helped.'}, token=org['token'])
    row, = await rows()
    assert result == {'review_id': row.id, 'status': 'received'}
    assert row.usefulness == 'partly'


async def test_mcp_review_schema():
    from treg.mcp import mcp, directory_mcp
    from treg.feedback_contract import REVIEW_ROWS_DESCRIPTION, REVIEW_USEFULNESS

    tool = next(tool for tool in await mcp.list_tools() if tool.name == 'review')
    assert tool.description == REVIEW_ROWS_DESCRIPTION
    assert 'rows' in tool.input_schema['properties']
    assert tool.input_schema['properties']['usefulness']['enum'] == list(REVIEW_USEFULNESS)
    assert tool.input_schema['required'] == ['call_id', 'usefulness']
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is True
    assert tool.annotations.idempotent_hint is False
    # the directory listing was submitted without it
    assert 'review' not in {tool.name for tool in await directory_mcp.list_tools()}


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


async def marks_for(call_id='review-call'):
    from treg.models import CallMark
    async with session_maker() as db:
        return list((await db.execute(select(CallMark).where(CallMark.call_id == call_id))).scalars())


async def test_review_rows_become_one_mark_tied_to_the_review(clients):
    org = await seed(clients)
    response = await submit(clients, org, usefulness='partly', reason='Two rows were off.',
                            rows=[{'index': 2, 'field': 'email', 'verdict': 'wrong'},
                                  {'index': 5, 'verdict': 'missing'}])
    assert response.status_code == 201, response.text
    body = response.json()
    mark, = await marks_for()
    assert body == {'review_id': body['review_id'], 'status': 'received', 'mark_id': mark.id}
    assert (mark.review_id, mark.basis, mark.endpoint_id, mark.client) == (
        body['review_id'], 'impression', 'example.search', 'review-test')
    assert mark.items == [{'index': 2, 'field': 'email', 'verdict': 'wrong'},
                          {'index': 5, 'verdict': 'missing'}]
    # A repeated review keeps the first, adds no rows and names the first one's mark.
    again = await submit(clients, org, usefulness='not_useful', rows=[{'verdict': 'wrong'}])
    assert again.status_code == 200
    assert again.json() == {'review_id': body['review_id'], 'status': 'already_reviewed',
                            'mark_id': mark.id}
    assert len(await marks_for()) == 1


async def test_review_rows_take_a_stated_basis_and_refuse_unknown_keys(clients):
    org = await seed(clients)
    bad = await submit(clients, org, rows=[{'row': 2, 'verdict': 'wrong'}])
    assert bad.status_code == 422
    assert await marks_for() == []
    ok = await submit(clients, org, usefulness='partly', basis='bounced',
                      rows=[{'index': 0, 'field': 'email', 'verdict': 'wrong'}])
    assert ok.status_code == 201, ok.text
    mark, = await marks_for()
    assert mark.basis == 'bounced'


async def test_review_without_rows_writes_no_mark(clients):
    org = await seed(clients)
    assert (await submit(clients, org)).json()['status'] == 'received'
    assert await marks_for() == []


async def test_mcp_review_relays_rows(clients):
    from test_mcp import _call_tool as team_call, mcp_session

    org = await seed(clients)
    async with mcp_session(clients) as client:
        result = await team_call(client, 'review', {
            'call_id': 'review-call', 'usefulness': 'partly',
            'rows': [{'index': 1, 'field': 'title', 'verdict': 'stale'}]}, token=org['token'])
    mark, = await marks_for()
    assert result['mark_id'] == mark.id
    assert mark.items == [{'index': 1, 'field': 'title', 'verdict': 'stale'}]


async def _call_schema(client, token):
    from test_mcp import _rpc
    await _rpc(client, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                      'clientInfo': {'name': 'test', 'version': '1'}}, token)
    tools = (await _rpc(client, 'tools/list', {}, token)).json()['result']['tools']
    return next(t for t in tools if t['name'] == 'call')['inputSchema']


@pytest.mark.parametrize('rate', [0, 1])
async def test_prior_review_rides_the_next_call_only_when_shown(clients, monkeypatch, rate):
    from test_mcp import _call_tool as team_call, mcp_session

    monkeypatch.setattr(get_settings(), 'mcp_prior_review_rate', rate)
    org = await seed(clients)
    prior = {'call_id': 'review-call', 'usefulness': 'partly',
             'rows': [{'index': 0, 'field': 'email', 'verdict': 'wrong'}]}
    async with mcp_session(clients) as client:
        schema = await _call_schema(client, org['token'])
        assert ('prior' in schema['properties']) is bool(rate)
        await team_call(client, 'call', {'endpoint_id': 'nope.search', 'prior': prior}, token=org['token'])
        await _priors_done()
    if rate:
        row, = await rows()
        assert (row.client, row.usefulness) == ('mcp-prior', 'partly')
        mark, = await marks_for()
        assert mark.review_id == row.id and mark.items == [{'index': 0, 'field': 'email', 'verdict': 'wrong'}]
    else:
        assert await rows() == [] and await marks_for() == []


async def test_a_bad_prior_review_never_fails_the_call(clients, monkeypatch):
    from test_mcp import _call_tool as team_call, mcp_session

    monkeypatch.setattr(get_settings(), 'mcp_prior_review_rate', 1)
    org = await seed(clients)
    async with mcp_session(clients) as client:
        plain = await team_call(client, 'call', {'endpoint_id': 'nope.search'}, token=org['token'])
        with_prior = await team_call(client, 'call', {
            'endpoint_id': 'nope.search',
            'prior': {'call_id': 'not-a-call-of-ours', 'usefulness': 'useful'}}, token=org['token'])
        await _priors_done()
    assert with_prior == plain
    assert await rows() == []


@pytest.mark.parametrize('prior,rate', [
    ('unreviewed', 1), (['review-call'], 1),
    ({'call_id': 'review-call', 'usefulness': 'partly', 'reason': 'x' * 201}, 1),
    ({'call_id': 'review-call', 'usefulness': 'partly', 'rows': [{'row': 2, 'verdict': 'wrong'}]}, 1),
    ({'call_id': 'review-call', 'usefulness': 'great'}, 0),
])
async def test_a_malformed_prior_review_is_dropped_and_the_call_runs(clients, monkeypatch, prior, rate):
    from test_mcp import _call_tool as team_call, mcp_session

    monkeypatch.setattr(get_settings(), 'mcp_prior_review_rate', rate)
    org = await seed(clients)
    async with mcp_session(clients) as client:
        plain = await team_call(client, 'call', {'endpoint_id': 'nope.search'}, token=org['token'])
        with_prior = await team_call(client, 'call', {'endpoint_id': 'nope.search', 'prior': prior},
                                     token=org['token'])
        await _priors_done()
    assert with_prior == plain
    assert await rows() == []


async def _priors_done():
    import asyncio
    from treg import mcp
    while mcp._PRIOR_TASKS:
        await asyncio.gather(*list(mcp._PRIOR_TASKS), return_exceptions=True)


async def test_a_slow_prior_review_does_not_hold_the_call(clients, monkeypatch):
    import asyncio
    import time
    from treg import mcp
    from test_mcp import _call_tool as team_call, mcp_session

    monkeypatch.setattr(get_settings(), 'mcp_prior_review_rate', 1)
    monkeypatch.setattr(mcp, 'PRIOR_TIMEOUT_S', 0.5)
    started = asyncio.Event()

    async def hang(prior, token):
        started.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(mcp, '_submit_prior', hang)
    org = await seed(clients)
    async with mcp_session(clients) as client:
        began = time.monotonic()
        await team_call(client, 'call', {'endpoint_id': 'nope.search',
                                         'prior': {'call_id': 'review-call', 'usefulness': 'useful'}},
                        token=org['token'])
        assert time.monotonic() - began < 2
        assert started.is_set() and mcp._PRIOR_TASKS
        for task in list(mcp._PRIOR_TASKS):
            task.cancel()
        await asyncio.gather(*list(mcp._PRIOR_TASKS), return_exceptions=True)


async def test_the_real_submission_gives_up_after_its_timeout(clients, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from treg import mcp

    monkeypatch.setattr(mcp, 'PRIOR_TIMEOUT_S', 0.05)

    class Slow:
        async def post(self, *args, **kw):
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def slow_api(token, *, client_name='mcp'):
        yield Slow()
    monkeypatch.setattr(mcp, '_api', slow_api)
    await asyncio.wait_for(mcp._submit_prior(mcp.PriorReviewIn(call_id='c1', usefulness='useful'), 't'), 1)


async def test_review_rows_spend_the_mark_allowance(clients, monkeypatch):
    from treg.application import feedback as feedback_app

    monkeypatch.setattr(feedback_app, 'MARK_RATE_MAX', 1)
    org = await seed(clients)
    async with session_maker() as db:
        db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev', tool_name='example',
                          method='GET', path='/', call_ref='second-call', status_code=200,
                          endpoint_id='example.search', provider='example'))
        await db.commit()
    marked = await clients.post('/marks', headers={'X-Treg-Token': org['token']},
                                json={'call_id': 'second-call', 'basis': 'bounced',
                                      'items': [{'verdict': 'wrong'}]})
    assert marked.status_code == 201, marked.text
    refused = await submit(clients, org, usefulness='partly', rows=[{'verdict': 'wrong'}])
    assert refused.status_code == 429
    assert await rows() == [] and await marks_for() == []
    # Without rows the review spends nothing and is saved.
    assert (await submit(clients, org, usefulness='partly')).status_code == 201
