"""Result admission through the real /call path, with only the upstream substituted."""
import json

import pytest
from sqlalchemy import select

from treg import archive
from treg.application.call import service
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot
from tests.test_marketplace_call import _fake_relay

EP = 'hunter.companies.emails'
URL = f'/call/{EP}?domain=example.com'

@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'hunter')
    monkeypatch.setenv('TREG_PLATFORM_KEY_HUNTER', 'test-platform-key')
    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, 'archive_mode', 'serve')
    monkeypatch.setattr(s, 'archive_serve_endpoints', EP)
    monkeypatch.setattr(s, 'archive_serve_percent', 100)
    yield
    get_settings.cache_clear()

async def test_empty_results_never_serve_or_extend_ttl(clients, cache_on, monkeypatch):
    raw = b'{"data":{"emails":[]},"meta":{"results":0}}'
    monkeypatch.setattr(service, 'relay', _fake_relay(200, raw))
    for _ in range(3):
        r = await clients.get(URL)
        assert r.status_code == 200 and r.content == raw
        assert r.headers.get('x-treg-cache') != 'hit'
        await archive.drain()
    async with session_maker() as s:
        k = (await s.execute(select(ArchiveKey))).scalar_one()
        assert k.stable_seen == k.change_seen == 0
        assert k.ttl_s == 604800
        snapshots = (await s.execute(select(ArchiveSnapshot))).scalars().all()
        assert len(snapshots) == 3  # history remains available


FOUND = b'{"data":{"emails":[{"value":"hello@example.com"}]},"meta":{"results":1}}'
EMPTY = b'{"data":{"emails":[]},"meta":{"results":0}}'


async def _call(clients, monkeypatch, raw, *, live=False, status=200):
    monkeypatch.setattr(service, 'relay', _fake_relay(status, raw))
    response = await clients.get(URL, headers={'Cache-Control': 'no-cache'} if live else {})
    await archive.drain()
    return response


async def _key():
    async with session_maker() as s:
        return (await s.execute(select(ArchiveKey))).scalar_one()


async def test_positive_cache_preserves_bytes_billing_and_history(clients, cache_on, monkeypatch):
    from treg import audit
    first = await _call(clients, monkeypatch, FOUND)
    second = await _call(clients, monkeypatch, EMPTY)
    assert second.headers['x-treg-cache'] == 'hit'
    assert first.content == second.content == FOUND
    k = await _key()
    assert k.stable_seen == k.change_seen == 0  # serving is not observation
    await audit.drain()
    rows = (await clients.get('/calls')).json()
    assert len(rows) == 2 and rows[0]['cached'] and not rows[1]['cached']
    for row in rows:
        result = (await clients.get(f"/calls/{row['id']}/result")).json()
        assert result['response']['body_text'] == FOUND.decode()
    from treg.models import CallRecord
    async with session_maker() as s:
        records = (await s.execute(select(CallRecord))).scalars().all()
        assert len(records) == 2
        assert records[0].cost_charged_micro == records[1].cost_charged_micro



async def test_disappearance_invalidates_once_and_retains_history(clients, cache_on, monkeypatch):
    await _call(clients, monkeypatch, FOUND)
    initial = (await _key()).ttl_s
    await _call(clients, monkeypatch, EMPTY, live=True)
    for _ in range(2):
        r = await _call(clients, monkeypatch, EMPTY)
        assert r.content == EMPTY and 'x-treg-cache' not in r.headers
    k = await _key()
    assert (k.stable_seen, k.change_seen, k.ttl_s, k.result_state) == (0, 1, initial // 2, 'empty')
    async with session_maker() as s:
        snapshots = (await s.execute(select(ArchiveSnapshot).order_by(ArchiveSnapshot.version))).scalars().all()
        assert len(snapshots) == 4
        assert snapshots[2].body_of == snapshots[3].body_of == snapshots[1].id
    old = await archive.resolve_result(k.key_hash, archive.content_hash(FOUND))
    assert old['response']['body_text'] == FOUND.decode()


@pytest.mark.parametrize('raw', [b'{"errors":[{"details":"upstream unavailable"}]}', b'{}', b'not JSON'])
async def test_uncertain_result_does_not_replace_or_refresh_positive(clients, cache_on, monkeypatch, raw):
    from datetime import timedelta
    await _call(clients, monkeypatch, FOUND)
    async with session_maker() as s:
        snap = (await s.execute(select(ArchiveSnapshot))).scalar_one()
        snap.fetched_at -= timedelta(seconds=120)
        s.add(snap)
        await s.commit()
    before = await _key()
    r = await _call(clients, monkeypatch, raw, live=True)
    assert r.content == raw
    after = await _key()
    assert (after.result_snapshot_id, after.ttl_s, after.stable_seen, after.change_seen) == (
        before.result_snapshot_id, before.ttl_s, 0, 0)
    r = await _call(clients, monkeypatch, EMPTY)
    assert r.content == FOUND and r.headers['x-treg-cache'] == 'hit'
    # The error's fresh timestamp must not renew the retained positive result.
    async with session_maker() as s:
        snap = await s.get(ArchiveSnapshot, before.result_snapshot_id)
        snap.fetched_at -= timedelta(days=8)
        s.add(snap)
        await s.commit()
    r = await _call(clients, monkeypatch, EMPTY)
    assert r.content == EMPTY and 'x-treg-cache' not in r.headers


@pytest.mark.parametrize('raw,expected_hit', [(EMPTY, False), (FOUND, True)])
async def test_legacy_cache_is_classified_before_serving(clients, cache_on, monkeypatch, raw, expected_hit):
    await _call(clients, monkeypatch, raw)
    async with session_maker() as s:
        k = (await s.execute(select(ArchiveKey))).scalar_one()
        k.result_state = k.result_snapshot_id = None
        s.add(k)
        await s.commit()
    r = await _call(clients, monkeypatch, raw)
    assert (r.headers.get('x-treg-cache') == 'hit') == expected_hit
    k = await _key()
    assert k.stable_seen == k.change_seen == 0


async def test_learning_compares_across_errors_and_recovers_after_empty(clients, cache_on, monkeypatch):
    await _call(clients, monkeypatch, FOUND)
    initial = (await _key()).ttl_s
    await _call(clients, monkeypatch, b'{}', live=True)
    await _call(clients, monkeypatch, FOUND, live=True)
    assert (await _key()).ttl_s == initial * 3 // 2
    await _call(clients, monkeypatch, EMPTY, live=True)
    await _call(clients, monkeypatch, FOUND)
    k = await _key()
    assert (k.stable_seen, k.change_seen, k.result_state) == (1, 2, 'found')
    assert (await _call(clients, monkeypatch, EMPTY)).content == FOUND


async def test_result_and_admission_telemetry(clients, cache_on, monkeypatch):
    events = []
    monkeypatch.setattr(service.analytics, 'capture',
                        lambda who, event, props, **kw: events.append((event, props)))
    for raw in (EMPTY, EMPTY, b'{}', b'{"error":"temporary"}', FOUND, FOUND):
        await _call(clients, monkeypatch, raw)
    props = [p for event, p in events if event == 'tool_called']
    assert [p['result_state'] for p in props] == ['empty', 'empty', 'unknown', 'error', 'found', 'found']
    assert [p.get('hit') for p in props] == [False, False, None, None, True, True]
    assert props[1]['cache_outcome'] == 'result_empty'
    assert props[-1]['cache_outcome'] == 'hit'
    assert props[2]['cache_admission'] == 'unknown'
    assert props[3]['result_reason'] == 'provider_error'
    assert all('body' not in p and 'key_hash' not in p for p in props)


@pytest.mark.parametrize('ep,body,state', [
    (EP, {'data': {'emails': []}}, 'empty'),
    (EP, {'data': {}}, 'unknown'),
    (EP, {'data': {'emails': [None]}}, 'unknown'),
    (EP, {'data': {'emails': [{'value': ''}]}}, 'unknown'),
    (EP, {'errors': [{'id': 'rate_limit'}], 'data': {'emails': []}}, 'error'),
    ('leadmagic.x.employee-finder', {'data': []}, 'empty'),
    ('leadmagic.x.employee-finder', {'data': [{'full_name': 'Person', 'has_email': False}]}, 'found'),
    ('leadmagic.x.employee-finder', {'data': [{'full_name': None}]}, 'unknown'),
    ('leadmagic.x.employee-finder', {'message': 'failure'}, 'unknown'),
    ('seranking.google.keywords.volume', [], 'empty'),
    ('seranking.google.keywords.volume', [{'is_data_found': False}], 'empty'),
    ('seranking.google.keywords.volume', [{'is_data_found': True, 'keyword': 'term', 'volume': 0}], 'found'),
    ('seranking.google.keywords.volume', [{'is_data_found': False}, {'is_data_found': True, 'keyword': 'term', 'volume': 10}], 'found'),
    ('seranking.google.keywords.volume', [{'is_data_found': 'false'}], 'unknown'),
    ('seranking.google.keywords.volume', [{'is_data_found': True}], 'unknown'),
    ('seranking.google.keywords.volume', [{'is_data_found': True, 'keyword': 'term', 'volume': True}], 'unknown'),
    ('unclassified.endpoint', {'data': [1]}, 'unknown'),
])
def test_conservative_result_rules(ep, body, state):
    from treg.domain.catalog.results import classify
    result = classify(ep, 201, json.dumps(body).encode())
    assert result.state == state
    assert result.hit == {'found': True, 'empty': False}.get(state)


@pytest.mark.parametrize('ep,request_body,empty', [
    ('seranking.google.keywords.volume', {'keywords': ['seo platform']}, [{'is_data_found': False}]),
    ('leadmagic.x.employee-finder', {'company_domain': 'example.com'}, {'data': []}),
])
async def test_other_candidates_through_real_call(clients, cache_on, monkeypatch, ep, request_body, empty):
    from pathlib import Path
    settings = get_settings()
    provider = ep.split('.')[0]
    monkeypatch.setattr(settings, 'platform_providers', provider)
    monkeypatch.setattr(settings, 'platform_key_' + provider, 'test-platform-key')
    monkeypatch.setattr(settings, 'archive_serve_endpoints', ep)
    raw = (Path(__file__).parents[1] / 'src/treg/catalog/examples' / (ep + '.json')).read_bytes()
    url = '/call/' + ep + ('?source=us' if provider == 'seranking' else '')
    monkeypatch.setattr(service, 'relay', _fake_relay(201, raw))
    first = await clients.post(url, json=request_body)
    assert first.status_code == 201 and first.content == raw
    await archive.drain()
    second = await clients.post(url, json=request_body)
    assert second.headers.get('x-treg-cache') == 'hit' and second.content == raw
    monkeypatch.setattr(service, 'relay', _fake_relay(200, json.dumps(empty).encode()))
    await clients.post(url, json=request_body, headers={'Cache-Control': 'no-cache'})
    await archive.drain()
    third = await clients.post(url, json=request_body)
    assert third.status_code == 200 and third.json() == empty
    assert 'x-treg-cache' not in third.headers


async def test_old_writer_cannot_leave_a_stale_positive_pointer(clients, cache_on, monkeypatch):
    await _call(clients, monkeypatch, FOUND)
    async with session_maker() as s:
        key = (await s.execute(select(ArchiveKey))).scalar_one()
        # Simulate the old binary appending an empty response during a rolling deployment.
        s.add(ArchiveSnapshot(key_id=key.id, version=2, status_code=200,
                              media_type='application/json', content_hash=archive.content_hash(EMPTY),
                              body=EMPTY, size_bytes=len(EMPTY)))
        await s.commit()
    r = await _call(clients, monkeypatch, EMPTY)
    assert r.content == EMPTY and 'x-treg-cache' not in r.headers
    key = await _key()
    assert key.result_state == 'empty' and key.result_observed_version == 3
    assert key.stable_seen == key.change_seen == 0  # no backfilled statistical evidence


async def test_pruner_protects_decisive_body_and_carrier(clients, cache_on, monkeypatch):
    from datetime import timedelta
    await _call(clients, monkeypatch, FOUND)
    await _call(clients, monkeypatch, FOUND, live=True)  # decisive snapshot references v1
    for n in range(4):
        await _call(clients, monkeypatch, json.dumps({'error': str(n)}).encode(), live=True)
    async with session_maker() as s:
        key = (await s.execute(select(ArchiveKey))).scalar_one()
        key.last_requested_at -= timedelta(days=40)
        s.add(key)
        for snap in (await s.execute(select(ArchiveSnapshot))).scalars():
            snap.fetched_at -= timedelta(days=40)
            s.add(snap)
        await s.commit()
    assert await archive.prune_once() > 0
    async with session_maker() as s:
        key = (await s.execute(select(ArchiveKey))).scalar_one()
        snap = await s.get(ArchiveSnapshot, key.result_snapshot_id)
        assert await archive._snapshot_body(s, snap) == FOUND


async def test_endpoint_without_hit_miss_keeps_original_cache_and_learning(clients, cache_on, monkeypatch):
    ep = 'hunter.x.domain-finder'
    monkeypatch.setattr(get_settings(), 'archive_serve_endpoints', ep)
    raw = b'{"data":{"domain":"example.com"}}'
    monkeypatch.setattr(service, 'relay', _fake_relay(200, raw))
    url = '/call/' + ep + '?company=Example'
    for _ in range(2):
        r = await clients.get(url, headers={'Cache-Control': 'no-cache'})
        assert r.status_code == 200 and r.content == raw
        await archive.drain()
    key = await _key()
    assert key.stable_seen == 1 and key.change_seen == 0
    assert key.ttl_s == 5400 and key.result_state is None
    r = await clients.get(url)
    assert r.headers.get('x-treg-cache') == 'hit' and r.content == raw


async def test_existing_verified_adapter_also_rejects_misses(clients, cache_on, monkeypatch):
    ep = 'hunter.companies.enrich'
    monkeypatch.setattr(get_settings(), 'archive_serve_endpoints', ep)
    raw = b'{"data":{"name":null}}'
    monkeypatch.setattr(service, 'relay', _fake_relay(200, raw))
    for _ in range(2):
        r = await clients.get('/call/' + ep + '?domain=example.com')
        assert r.status_code == 200 and r.content == raw
        assert 'x-treg-cache' not in r.headers
        await archive.drain()
    assert (await _key()).result_state == 'empty'


async def test_unverified_hit_miss_does_not_enable_new_policy(clients, cache_on, monkeypatch):
    from dataclasses import replace
    from treg.domain.catalog.store import load
    from treg.domain.catalog.results import has_result_rules
    cat = load()
    monkeypatch.setitem(cat.adapters, EP, replace(cat.adapters[EP], verified=False))
    assert not has_result_rules(EP)
    events = []
    monkeypatch.setattr(service.analytics, 'capture',
                        lambda who, event, props, **kw: events.append((event, props)))
    await _call(clients, monkeypatch, EMPTY)
    r = await _call(clients, monkeypatch, FOUND)
    # Preserves the original behavior for endpoints whose hit/miss is not enabled.
    assert r.content == EMPTY and r.headers.get('x-treg-cache') == 'hit'
    props = [p for event, p in events if event == 'tool_called']
    assert all(p['cache_result_policy'] == 'legacy' and p['cache_admission'] == 'not_applicable' for p in props)


@pytest.mark.parametrize('ep', ['hunter.companies.enrich', 'tikhub.tiktok.video.comments'])
def test_verified_adapter_positive_fixture_is_admissible(ep):
    from pathlib import Path
    from treg.domain.catalog.results import classify, has_result_rules
    assert has_result_rules(ep)
    body = (Path(__file__).parents[1] / 'src/treg/catalog/examples' / (ep + '.json')).read_bytes()
    result = classify(ep, 200, body)
    assert result.state == 'found' and result.reason == 'adapter_hit'


async def test_ignore_cannot_mask_result_transitions_and_uses_decisive_baseline(clients, cache_on, monkeypatch):
    from treg.domain.catalog import store
    monkeypatch.setitem(store.load().by_id[EP], 'cache',
                        {'mode': 'transient', 'ignore_paths': ['meta', 'data']})
    events = []
    monkeypatch.setattr(service.analytics, 'capture',
                        lambda who, event, props, **kw: events.append((event, props)))
    await _call(clients, monkeypatch, FOUND)
    await _call(clients, monkeypatch, b'{}', live=True)  # unknown, retain found baseline
    changed = FOUND.replace(b'hello@', b'other@')
    await _call(clients, monkeypatch, changed, live=True)
    assert (await _key()).stable_seen == 1
    await _call(clients, monkeypatch, EMPTY, live=True)
    k = await _key()
    assert (k.stable_seen, k.change_seen, k.result_state) == (1, 1, 'empty')
    observed = [p for name, p in events if name == 'archive_change_observed']
    # Unknown evidence does not drive learning or produce a comparison event. The rescued
    # found comparison must describe the decisive found body, not the intervening empty object.
    assert [p['masked_by_ignore'] for p in observed] == [True, False]
    assert observed[0]['changed_paths'] == ['data.emails[*].value']
    assert observed[0]['path_count'] == 1
    assert observed[0]['sole_path'] == 'data.emails[*].value'
    assert observed[1]['changed_paths'] == ['data.emails[*]', 'meta.results']
    await _call(clients, monkeypatch, EMPTY)
    assert (await _key()).change_seen == 1
    response = await _call(clients, monkeypatch, changed)
    assert response.content == changed and 'x-treg-cache' not in response.headers
    assert (await _key()).change_seen == 2


@pytest.mark.parametrize("raw", [
    b'{"status":"succeeded","email":""}',
    b'{"status":"failed","email":"person@example.com"}',
    b'{"status":"succeeded","email":123}',
    b'{"status":"succeeded","email":"not-an-email"}',
    b'{"status":"not_found"}',
])
async def test_leadsforge_invalid_results_never_serve(clients, monkeypatch, raw):
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'leadsforge')
    monkeypatch.setenv('TREG_PLATFORM_KEY_LEADSFORGE', 'test-key')
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, 'archive_mode', 'serve')
    monkeypatch.setattr(settings, 'archive_serve_endpoints', 'leadsforge.people.email.find')
    monkeypatch.setattr(settings, 'archive_serve_percent', 100)
    try:
        monkeypatch.setattr(service, 'relay', _fake_relay(200, raw))
        for _ in range(2):
            r = await clients.post('/call/leadsforge.people.email.find', json={'personID':'synthetic-person'})
            await archive.drain()
            assert r.status_code == 200 and r.content == raw
            assert r.headers.get('x-treg-cache') != 'hit'
    finally:
        get_settings.cache_clear()


async def test_operator_cap_bounds_existing_ttl_and_preserves_bypass(clients, cache_on, monkeypatch):
    from datetime import timedelta
    settings = get_settings()
    monkeypatch.setattr(settings, 'archive_serve_max_age_s', {EP: 86400})
    await _call(clients, monkeypatch, FOUND)
    async with session_maker() as s:
        k = (await s.execute(select(ArchiveKey))).scalar_one()
        k.ttl_s = 30 * 86400
        snap = await s.get(ArchiveSnapshot, k.result_snapshot_id)
        snap.fetched_at -= timedelta(hours=23)
        s.add(k)
        s.add(snap)
        await s.commit()
    assert (await _call(clients, monkeypatch, EMPTY)).headers['x-treg-cache'] == 'hit'
    async with session_maker() as s:
        snap = (await s.execute(select(ArchiveSnapshot))).scalar_one()
        snap.fetched_at -= timedelta(hours=2)
        s.add(snap)
        await s.commit()
    fresh = await _call(clients, monkeypatch, FOUND)
    assert 'x-treg-cache' not in fresh.headers
    bypass = await _call(clients, monkeypatch, EMPTY, live=True)
    assert bypass.content == EMPTY and 'x-treg-cache' not in bypass.headers


@pytest.mark.parametrize('body,state', [
    ({'status':'succeeded','email':'person@example.com'}, 'found'),
    ({'status':'not_found'}, 'empty'),
    ({'status':'not_found','email':'person@example.com'}, 'unknown'),
    ({'email':'person@example.com'}, 'unknown'),
    ({'status':'succeeded','email':' person@example.com'}, 'unknown'),
])
def test_leadsforge_result_contract(body, state):
    from treg.domain.catalog.results import classify
    assert classify('leadsforge.people.email.find', 200, json.dumps(body).encode()).state == state


async def test_default_json_comparison_preserves_call_bytes(clients, cache_on, monkeypatch):
    first = b'{"data":{"emails":[{"value":"hello@example.com"}]},"meta":{"results":1}}'
    reordered = b'{ "meta": {"results": 1}, "data": {"emails": [{"value": "hello@example.com"}]} }'
    await _call(clients, monkeypatch, first, live=True)
    response = await _call(clients, monkeypatch, reordered, live=True)
    assert response.content == reordered
    k = await _key()
    assert (k.stable_seen, k.change_seen) == (1, 0)
    cached = await _call(clients, monkeypatch, EMPTY)
    assert cached.headers['x-treg-cache'] == 'hit' and cached.content == reordered
    for raw in (first, reordered):
        result = await archive.resolve_result(k.key_hash, archive.content_hash(raw))
        assert result['response']['body_text'] == raw.decode()


@pytest.mark.parametrize('ep,container', [
    ('hunter.people.email.find', 'data'), ('findymail.search.name', 'contact'),
])
@pytest.mark.parametrize('email', ['', 'not-an-email', 123, ' person@example.com'])
async def test_email_pilot_rejects_malformed_positive(clients, monkeypatch, ep, container, email):
    provider = ep.split('.')[0]
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', provider)
    monkeypatch.setenv('TREG_PLATFORM_KEY_' + provider.upper(), 'test-key')
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, 'archive_mode', 'serve')
    monkeypatch.setattr(settings, 'archive_serve_endpoints', ep)
    monkeypatch.setattr(settings, 'archive_serve_percent', 100)
    raw = json.dumps({container: {'email': email}}).encode()
    monkeypatch.setattr(service, 'relay', _fake_relay(200, raw))
    try:
        for _ in range(2):
            if provider == 'hunter':
                r = await clients.get('/call/' + ep + '?domain=example.com&full_name=Test')
            else:
                r = await clients.post('/call/' + ep, json={'name': 'Test', 'domain': 'example.com'})
            await archive.drain()
            assert r.status_code == 200 and r.content == raw
            assert 'x-treg-cache' not in r.headers
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize('ep,field', [
    ('hunter.people.email.find', 'data'), ('findymail.search.name', 'contact'),
])
async def test_email_pilot_positive_and_empty_admission(clients, monkeypatch, ep, field):
    provider = ep.split('.')[0]
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', provider)
    monkeypatch.setenv('TREG_PLATFORM_KEY_' + provider.upper(), 'test-key')
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, 'archive_mode', 'serve')
    monkeypatch.setattr(settings, 'archive_serve_endpoints', ep)
    monkeypatch.setattr(settings, 'archive_serve_percent', 100)
    async def call(raw, live=False):
        monkeypatch.setattr(service, 'relay', _fake_relay(200, raw))
        kwargs = {'headers': {'Cache-Control': 'no-cache'} if live else {}}
        if provider == 'hunter':
            r = await clients.get('/call/' + ep + '?domain=example.com&full_name=Test', **kwargs)
        else:
            r = await clients.post('/call/' + ep, json={'name': 'Test', 'domain': 'example.com'}, **kwargs)
        await archive.drain()
        return r
    found = json.dumps({field: {'email': 'person@example.com'}}).encode()
    empty = json.dumps({field: {'email': None}}).encode()
    try:
        await call(found)
        cached = await call(empty)
        assert cached.headers['x-treg-cache'] == 'hit' and cached.content == found
        await call(empty, live=True)
        for _ in range(2):
            r = await call(empty)
            assert 'x-treg-cache' not in r.headers and r.content == empty
        k = await _key()
        assert (k.stable_seen, k.change_seen, k.result_state) == (0, 1, 'empty')
    finally:
        get_settings.cache_clear()


async def test_hunter_declared_date_ignore_preserves_verification_status(clients, cache_on, monkeypatch):
    def body(date, status='valid'):
        return json.dumps({'data': {'emails': [{'value': 'person@example.com',
                          'verification': {'date': date, 'status': status}}]}}).encode()
    await _call(clients, monkeypatch, body('2026-09-01'), live=True)
    await _call(clients, monkeypatch, body('2026-09-02'), live=True)
    assert ((await _key()).stable_seen, (await _key()).change_seen) == (1, 0)
    await _call(clients, monkeypatch, body('2026-09-03', 'invalid'), live=True)
    assert ((await _key()).stable_seen, (await _key()).change_seen) == (1, 1)


@pytest.mark.parametrize('ep,body,state', [
    ('hunter.people.email.find', {}, 'unknown'),
    ('hunter.people.email.find', {'data': {}}, 'unknown'),
    ('hunter.people.email.find', {'data': None}, 'unknown'),
    ('hunter.people.email.find', {'data': {'email': None}}, 'empty'),
    ('findymail.search.name', {}, 'unknown'),
    ('findymail.search.name', {'contact': {}}, 'unknown'),
    ('findymail.search.name', {'contact': None}, 'empty'),
])
def test_email_pilot_missing_fields(ep, body, state):
    from treg.domain.catalog.results import classify
    assert classify(ep, 200, json.dumps(body).encode()).state == state
