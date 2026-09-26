"""Pinned agents share a wallet, but never another pin's history or shared-provider objects."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from treg import archive, audit
from treg.application import asynctasks as task_app
from treg.application.call import service as call_service
from treg.domain import money
from treg.infra.db import session_maker
from treg.models import AsyncResourceRecord, AsyncTaskRecord, CallRecord, Feedback, LedgerEntry, RunRecord
from treg.timeutil import utcnow_naive
from test_asynctasks import EP, _response, replicate_platform, legacy_async_platform, minimax_platform
from test_tag_billing import _mint_agent, _mk_echo_tool, _org_id
from test_run import _register_runnable
from test_marketplace_call import platform_on  # noqa: F401
from test_routing import ROUTED, _relay_by_provider, enrichment_on  # noqa: F401


@pytest.fixture
async def identities(clients):
    org = await _org_id(clients)
    result = {}
    for name, tags in [('a', {'customer': 'a'}), ('b', {'customer': 'b'}),
                       ('aw', {'customer': 'a', 'workspace': 'w'})]:
        token = await _mint_agent(clients, org, name, role='viewer', pinned_tags=tags)
        result[name] = {'X-Treg-Token': token}
    return org, result


async def test_history_filters_before_pagination_and_ignores_read_headers(clients, identities, monkeypatch):
    org, h = identities
    await _mk_echo_tool(clients)
    # The caller owns two separated pages; newer foreign/untagged rows must not consume the limit.
    for who in ['a', 'a', 'b', None]:
        r = await clients.get('/call/echo/hello', headers=h[who] if who else {})
        assert r.status_code == 200
    await audit.drain()
    all_rows = (await clients.get('/calls')).json()
    assert len(all_rows) == 4
    own = [r for r in all_rows if r['tags'] == {'customer': 'a'}]
    first = (await clients.get('/calls?limit=1', headers=h['a'])).json()
    assert [r['id'] for r in first] == [own[0]['id']]
    second = (await clients.get(f"/calls?limit=1&before_id={first[0]['id']}", headers=h['a'])).json()
    assert [r['id'] for r in second] == [own[1]['id']]
    assert (await clients.get('/calls', headers=h['aw'])).json() == []

    async def must_not_load(*args):
        raise AssertionError('foreign archive bytes loaded')
    monkeypatch.setattr(archive, 'resolve_result', must_not_load)
    for row in all_rows:
        expected = 200 if row in own else 404
        for path in [f"/calls/{row['id']}/result", f"/calls/{row['call_ref']}"]:
            response = await clients.get(path, headers={**h['a'], 'X-Treg-Meta': 'customer=b'})
            assert response.status_code == expected, response.text
    # A matching second pin is required, not merely the primary customer tag.
    response = await clients.get('/call/echo/hello', headers={**h['a'], 'X-Treg-Meta': 'workspace=w'})
    assert response.status_code == 200
    await audit.drain()
    assert len((await clients.get('/calls', headers=h['aw'])).json()) == 1


async def test_archive_content_is_loaded_only_after_pin_authorization(clients, identities, monkeypatch):
    org, h = identities
    async with session_maker() as db:
        row = CallRecord(org_id=org, user_email='synthetic@example.invalid', tool_name='catalog',
                         method='GET', path='/', status_code=200, tags={'customer': 'a'},
                         archive_key_hash='key', archive_content_hash='body')
        db.add(row)
        await db.commit()
        row_id = row.id
    loads = []
    async def load(key, body):
        loads.append((key, body))
        return {'stored': True, 'request': {'query': 'a'}, 'response': {'answer': 'a'}}
    monkeypatch.setattr(archive, 'resolve_result', load)
    assert (await clients.get(f'/calls/{row_id}/result', headers=h['b'])).status_code == 404
    assert loads == []
    response = await clients.get(f'/calls/{row_id}/result', headers=h['a'])
    assert response.status_code == 200 and response.json()['response'] == {'answer': 'a'}
    assert loads == [('key', 'body')]


@pytest.mark.parametrize('finish', ['settle', 'release'])
async def test_ledger_only_read_uses_immutable_reserve_tags(clients, identities, finish):
    org, h = identities
    async with session_maker() as db:
        call_id = await money.reserve(db, org, 'synthetic', 100, tags={'customer': 'a'},
                                      meta={'tags': {'customer': 'b'}})
        await getattr(money, finish)(db, call_id)
    own = await clients.get(f'/calls/{call_id}', headers=h['a'])
    assert own.status_code == 200, own.text
    assert own.json()['call'] is None
    assert {e['kind'] for e in own.json()['ledger']} == {'reserve', finish}
    assert (await clients.get(f'/calls/{call_id}', headers=h['b'])).status_code == 404
    assert (await clients.get(f'/calls/{call_id}', headers=h['aw'])).status_code == 404
    assert (await clients.get(f'/calls/{call_id}')).status_code == 200
    # Existing unattributed ledger-only entries remain operator-readable, never guessed from users.
    async with session_maker() as db:
        entry = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == 'reserve'))).scalar_one()
        entry.meta = {}
        await db.commit()
    assert (await clients.get(f'/calls/{call_id}', headers=h['a'])).status_code == 404
    assert (await clients.get(f'/calls/{call_id}')).status_code == 200


async def test_async_submission_snapshots_tags_without_audit(clients, identities, monkeypatch, replicate_platform):
    org, h = identities
    monkeypatch.setattr(audit, 'record_call', lambda **kwargs: None)
    hits = []
    async def relay(request, *args, **kwargs):
        hits.append(request)
        return _response(201, {'id': 'task-a', 'status': 'starting'})
    monkeypatch.setattr(call_service, 'relay', relay)
    response = await clients.post(f'/call/{EP}', headers=h['a'], json={'input': {'prompt': 'kite'}})
    assert response.status_code == 201, response.text
    ref = response.headers['X-Treg-Call-Id']
    async with session_maker() as db:
        task = await db.get(AsyncTaskRecord, ref)
        assert task.tags == {'customer': 'a'}
        resources = (await db.execute(select(AsyncResourceRecord))).scalars().all()
        assert resources and all(r.tags == task.tags for r in resources)
    assert (await clients.get(f'/calls/{ref}', headers=h['a'])).json()['async_task']['task_id'] == 'task-a'
    assert (await clients.get(f'/calls/{ref}', headers=h['b'])).status_code == 404
    assert (await task_app.views_for(org, [ref], pinned_tags={'customer': 'b'})) == {}
    for token in ['b', 'aw']:
        for task_id in ['task-a', 'unknown']:
            denied = await clients.get(f'/call/replicate.predictions.get?id={task_id}', headers=h[token])
            assert denied.status_code == 404, denied.text
    assert len(hits) == 1
    assert (await clients.get('/call/replicate.predictions.get?id=task-a', headers=h['a'])).status_code == 201
    assert len(hits) == 2


async def test_legacy_async_rows_fail_closed_for_pins(clients, identities, monkeypatch, replicate_platform):
    org, h = identities
    now = utcnow_naive()
    from treg.domain.catalog import store
    async with session_maker() as db:
        db.add(AsyncTaskRecord(call_id='legacy', org_id=org, provider='replicate', endpoint_id=EP,
            task_id='old', reserved_micro=0, descriptor=store.load().by_id[EP]['async'],
            next_check_at=now + timedelta(hours=1)))
        await db.commit()
    calls = []
    async def relay(*args, **kwargs):
        calls.append(True)
        return _response(200, {'id': 'old', 'status': 'processing'})
    monkeypatch.setattr(call_service, 'relay', relay)
    assert (await clients.get('/call/replicate.predictions.get?id=old', headers=h['a'])).status_code == 404
    assert calls == []
    assert (await clients.get('/call/replicate.predictions.get?id=old')).status_code == 200


async def test_resource_creation_and_fetch_preserve_customer(clients, identities, monkeypatch, legacy_async_platform):
    org, h = identities
    hits = []
    async def relay(*args, **kwargs):
        hits.append(True)
        return _response(200, {'snapshot_id': 'snapshot-a'})
    monkeypatch.setattr(call_service, 'relay', relay)
    response = await clients.post('/call/brightdata.web.scrape.job.start?dataset_id=gd_test',
                                  json=[{'url': 'https://example.com'}], headers=h['a'])
    assert response.status_code == 200, response.text
    for path in ['/call/brightdata.web.scrape.job.status?snapshot_id=snapshot-a',
                 '/call/brightdata.web.scrape.job.results?snapshot_id=snapshot-a&format=json']:
        assert (await clients.get(path, headers=h['b'])).status_code == 404
        assert (await clients.get(path, headers=h['a'])).status_code == 200
    assert len(hits) == 3
    async with session_maker() as db:
        rows = (await db.execute(select(AsyncResourceRecord))).scalars().all()
        assert rows and all(r.tags == {'customer': 'a'} for r in rows)


async def test_runs_are_filtered_before_limits(clients, identities):
    org, h = identities
    async with session_maker() as db:
        for tags in [{'customer': 'a'}, {'customer': 'b'}, None]:
            db.add(RunRecord(org_id=org, user_email='synthetic@example.invalid', bundle_name='echo',
                             argv=['hello'], exit_code=0, duration_ms=1, tags=tags))
            db.add(CallRecord(org_id=org, user_email='synthetic@example.invalid', tool_name='echo',
                             method='GRANT', path='hello', status_code=200, kind='local_run', tags=tags))
        await db.commit()
    own = (await clients.get('/runs?limit=2', headers=h['a'])).json()
    assert len(own) == 2 and {r['where'] for r in own} == {'local', 'server'}
    assert len((await clients.get('/runs')).json()) == 6
    assert (await clients.get('/runs', headers=h['aw'])).json() == []


async def test_run_writers_snapshot_pins(clients, identities, monkeypatch):
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), 'run_proof', 'synthetic-proof')
    org, _ = identities
    await _register_runnable(clients)
    token = await _mint_agent(clients, org, 'runner', role='member',
                             pinned_tags={'customer': 'a'}, local_run_enabled=True)
    headers = {'X-Treg-Token': token}
    response = await clients.post('/run', headers=headers, json={'tool': 'sh-skill', 'args': ['-c', 'echo hi']})
    assert response.status_code == 200, response.text
    grant = await clients.post('/tools/sh-skill/grant', headers={**headers, 'X-Treg-Run-Proof': 'synthetic-proof'}, json={'argv': ['echo', 'hi']})
    assert grant.status_code == 200, grant.text
    await audit.drain()
    rows = (await clients.get('/runs', headers=headers)).json()
    assert len(rows) == 2 and {r['where'] for r in rows} == {'local', 'server'}


async def test_same_provider_idempotency_label_is_partitioned_by_pin(clients, identities, monkeypatch, replicate_platform):
    org, h = identities
    headers_seen = []
    async def relay(request, *args, **kwargs):
        headers_seen.append(dict(request.raw_headers)[b'idempotency-key'])
        return _response(201, {'id': f'task-{len(headers_seen)}', 'status': 'starting'})
    monkeypatch.setattr(call_service, 'relay', relay)
    for name in ['a', 'b', 'a']:
        response = await clients.post(f'/call/{EP}', headers={**h[name], 'Idempotency-Key': 'retry-1'},
                                      json={'input': {'prompt': 'kite'}})
        assert response.status_code == 201, response.text
    assert len(headers_seen) == 2 and headers_seen[0] != headers_seen[1]


@pytest.mark.parametrize('path, tool', [('/mcp/', 'call'), ('/mcp/v2/', 'catalog_call_read')])
async def test_mcp_async_reads_apply_the_same_pin_check(clients, identities, monkeypatch, replicate_platform, path, tool):
    from test_mcp_directory import paired_mcp_session, _call_tool
    org, h = identities
    async def relay(*args, **kwargs):
        return _response(201, {'id': 'mcp-task', 'status': 'starting'})
    monkeypatch.setattr(call_service, 'relay', relay)
    submitted = await clients.post(f'/call/{EP}', headers=h['a'], json={'input': {'prompt': 'kite'}})
    assert submitted.status_code == 201
    async def must_not_relay(*args, **kwargs):
        raise AssertionError('MCP crossed a customer pin')
    monkeypatch.setattr(call_service, 'relay', must_not_relay)
    async with paired_mcp_session() as client:
        result = await _call_tool(client, tool, {'endpoint_id': 'replicate.predictions.get',
                                  'params': {'id': 'mcp-task'}}, h['b']['X-Treg-Token'], path=path)
    assert result.get('status') == 404, result


async def test_terminal_resource_inherits_submission_tags(clients, identities, monkeypatch, minimax_platform):
    org, h = identities
    hits = []
    responses = [
        {'task_id': 'task-a', 'base_resp': {'status_code': 0}},
        {'status': 'Success', 'file_id': 'file-a'},
        {'file': {'download_url': 'https://example.invalid/video.mp4'}},
    ]
    async def relay(*args, **kwargs):
        hits.append(True)
        return _response(200, responses.pop(0))
    monkeypatch.setattr(call_service, 'relay', relay)
    submitted = await clients.post('/call/minimax.video-gen.from_text', headers=h['aw'], json={
        'model': 'MiniMax-Hailuo-2.3', 'prompt': 'A paper boat.', 'duration': 6, 'resolution': '768P'})
    assert submitted.status_code == 200, submitted.text
    # A broader customer-a token can poll; the new file must retain the original workspace pin.
    polled = await clients.get('/call/minimax.video-gen.task.status?task_id=task-a', headers=h['a'])
    assert polled.status_code == 200, polled.text
    async with session_maker() as db:
        resource = (await db.execute(select(AsyncResourceRecord).where(
            AsyncResourceRecord.resource_id == 'file-a'))).scalar_one()
        assert resource.tags == {'customer': 'a', 'workspace': 'w'}
    path = '/call/minimax.video-gen.result.retrieve?file_id=file-a'
    assert (await clients.get(path, headers=h['b'])).status_code == 404
    assert (await clients.get(path, headers=h['aw'])).status_code == 200
    assert len(hits) == 3


def test_replay_scope_includes_every_pin_and_preserves_unpinned_keys():
    from treg.application.call.idempotency import _scoped_idempotency_key
    from treg.application.call.intake import CallMeta
    meta = CallMeta(tags={'customer': 'a'}, primary_dim='customer')
    old = _scoped_idempotency_key('retry', meta)
    a = _scoped_idempotency_key('retry', meta, pinned_tags={'customer': 'a', 'workspace': 'a'})
    b = _scoped_idempotency_key('retry', meta, pinned_tags={'customer': 'a', 'workspace': 'b'})
    assert len({old, a, b}) == 3
    assert a == _scoped_idempotency_key('retry', meta, pinned_tags={'workspace': 'a', 'customer': 'a'})


async def test_repinning_cannot_read_or_replay_previous_scope(clients, identities, monkeypatch, replicate_platform):
    org, _ = identities
    hits = []
    async def relay(*args, **kwargs):
        hits.append(True)
        return _response(201, {'id': f'task-{len(hits)}', 'status': 'starting'})
    monkeypatch.setattr(call_service, 'relay', relay)
    for workspace in ['first', 'second']:
        token = await _mint_agent(clients, org, 'repinned', role='viewer',
                                  pinned_tags={'customer': 'a', 'workspace': workspace})
        headers = {'X-Treg-Token': token, 'Idempotency-Key': 'retry'}
        response = await clients.post(f'/call/{EP}', headers=headers, json={'input': {'prompt': 'kite'}})
        assert response.status_code == 201
        assert response.json()['id'] == f'task-{len(hits)}'
        await audit.drain()
        rows = (await clients.get('/calls', headers=headers)).json()
        assert rows and all(row['tags']['workspace'] == workspace for row in rows)
    assert len(hits) == 2


async def test_routed_parent_row_carries_the_pin_and_reviews_are_scoped(clients, identities, enrichment_on, monkeypatch):
    """A routed call writes its parent audit row outside `service.py`; it must carry the pin or
    the caller's own call disappears from the filtered history. A review is a read of the same
    row, so a foreign reference is a 404 for a pinned caller."""
    org, h = identities
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider(
        {'tomba': [(200, {'data': {'email': 'patrick@stripe.com', 'score': 99,
                                   'verification': {'status': 'valid'}}})]}, []))
    r = await clients.post(f'/call/{ROUTED}', json={'full_name': 'Patrick Collison', 'domain': 'stripe.com'},
                           headers=h['a'])
    assert r.status_code == 200, r.text
    ref = r.headers['X-Treg-Call-Id']
    await audit.drain()
    rows = (await clients.get('/calls', headers=h['a'])).json()
    assert {(x['tool_name'], x['credential_tier'], x['call_ref']) for x in rows} >= {(ROUTED, 'routed', ref)}
    assert all(x['tags'] == {'customer': 'a'} for x in rows)
    assert (await clients.get('/calls', headers=h['b'])).json() == []
    assert (await clients.get(f'/calls/{ref}', headers=h['a'])).json()['call']['tool_name'] == ROUTED
    review = {'call_id': ref, 'usefulness': 'useful'}
    assert (await clients.post('/reviews', json=review, headers=h['b'])).status_code == 404
    assert (await clients.post('/reviews', json=review, headers=h['a'])).status_code == 201


async def test_refusals_and_feedback_reports_stay_inside_the_pin(clients, identities):
    """A refusal is written by the router's fallback, not by `service.py`; it must carry the pin or
    the caller cannot see its own 403/404 while integrating. A feedback report snapshots the pin
    and is readable only through it, and verifies call references only against the pin's rows."""
    org, h = identities
    await _mk_echo_tool(clients)
    refs = []
    for headers, status in [(h['a'], 404), ({**h['a'], 'X-Treg-Meta': 'customer=b'}, 403)]:
        r = await clients.get('/call/no-such-tool/x' if status == 404 else '/call/echo/hello', headers=headers)
        assert r.status_code == status, r.text
        refs.append(r.headers['X-Treg-Call-Id'])
    await audit.drain()
    own = (await clients.get('/calls', headers=h['a'])).json()
    assert {x['call_ref'] for x in own} == set(refs) and all(x['tags'] == {'customer': 'a'} for x in own)
    assert (await clients.get('/calls', headers=h['b'])).json() == []
    for ref in refs:
        assert (await clients.get(f'/calls/{ref}', headers=h['a'])).status_code == 200
        assert (await clients.get(f'/calls/{ref}', headers=h['b'])).status_code == 404
    report = {'category': 'quality', 'message': 'customer a private note', 'call_ids': [refs[0]]}
    r = await clients.post('/feedback', json=report, headers=h['a'])
    assert r.status_code == 201, r.text
    fid = r.json()['feedback_id']
    assert (await clients.get(f'/feedback/{fid}', headers=h['a'])).status_code == 200
    assert (await clients.get(f'/feedback/{fid}', headers=h['b'])).status_code == 404
    assert (await clients.get(f'/feedback/{fid}')).status_code == 200, 'the unpinned operator still sees it'
    # b may cite a's reference, but it is a claim, never a verified one.
    r = await clients.post('/feedback', json={**report, 'message': 'foreign ref'}, headers=h['b'])
    assert r.status_code == 201, r.text
    async with session_maker() as db:
        rows = {f.message: f for f in (await db.execute(select(Feedback))).scalars().all()}
    assert rows['customer a private note'].verified_call_ids == [refs[0]]
    assert rows['foreign ref'].verified_call_ids == [] and rows['foreign ref'].tags == {'customer': 'b'}
