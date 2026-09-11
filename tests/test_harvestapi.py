"""Harvest's API-key-only catalog, shared opt-in constraints and reported-charge settlement."""
import json

import httpx
import pytest
from httpx import AsyncClient

from treg.api import app
from treg.config import get_settings
from treg.domain.catalog import store
from treg.domain.capacity import collectors
from treg.oauth_providers import get, platform_bindings

PROFILE = 'harvestapi.linkedin.user.profile'
COMPANY = 'harvestapi.linkedin.company.profile'


class _JSONStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


def response(status, *, json):
    # The real relay reads aiter_raw(); MockTransport's eager json= response is already consumed.
    import json as codec
    return httpx.Response(status, headers={"content-type": "application/json"},
                          stream=_JSONStream(codec.dumps(json).encode()))


@pytest.fixture
def harvest_on(monkeypatch):
    monkeypatch.setenv('TREG_PLATFORM_KEY_HARVESTAPI', 'TEST-PLATFORM-HARVEST')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'harvestapi')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def balance(client):
    org = (await client.get('/orgs')).json()[0]['org_id']
    return (await client.get(f'/orgs/{org}/balance')).json()


def test_harvest_catalog_is_read_only_and_platform_eligible(harvest_on):
    catalog = store.load()
    rows = [ep for ep in catalog.endpoints if ep['provider'] == 'harvestapi']
    assert len(rows) == 25
    assert len({ep['path'] for ep in rows}) == 23
    for ep in rows:
        assert ep['method'] == 'GET'
        assert ep['strict_query']
        assert catalog.platform_eligible(ep)
        assert ep['path'].startswith('/linkedin/')
        assert not {'cookie', 'proxy', 'userAgent', 'usePrivatePool', 'requiredAccountId', 'sessionId'} & ep['input']['queryParams'].keys()
    assert get_settings().platform_key_for('harvestapi') == 'TEST-PLATFORM-HARVEST'
    assert get('harvestapi').probe_path == '/users/my-api-user'
    assert platform_bindings(get('harvestapi'))[0]['name'] == 'X-API-Key'


async def test_harvest_connect_rejects_bad_key_and_accepts_empty_wallet(clients, monkeypatch):
    def probe(request):
        assert request.url.path == '/users/my-api-user'
        assert request.method == 'GET'
        if request.headers['x-api-key'] == 'bad':
            return response(401, json={'message': 'Invalid API key'})
        return response(200, json={'usage': {'balance': 0}, 'user': {'totalBalance': 20}})
    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        bad = await clients.post('/connections/token', json={'provider': 'harvestapi', 'token': 'bad'})
        assert bad.status_code == 422
        assert (await clients.get('/tools')).json() == []
        good = await clients.post('/connections/token', json={'provider': 'harvestapi', 'token': 'own-key'})
        assert good.status_code == 200, good.text
        tool = next(t for t in (await clients.get('/tools')).json() if t['name'] == 'harvestapi')
        assert tool['bindings'][0]['name'] == 'X-API-Key'


@pytest.mark.parametrize('own', [False, True])
@pytest.mark.parametrize('query', [
    'cookie=private', '%63ookie=private', 'usePrivatePool=true', 'requiredAccountId=x',
    'proxy=private', 'userAgent=private', 'sessionId=x', 'includeAboutProfile=true',
    'main=true', 'findEmail=true', 'publicIdentifier=a&publicIdentifier=b',
])
async def test_harvest_excluded_queries_never_reach_upstream_or_money(clients, monkeypatch, harvest_on, own, query):
    if own:
        await clients.post('/secrets', json={'name': 'harvestapi', 'value': 'TEST-OWN'})
    before = await balance(clients)
    def upstream(request):
        pytest.fail('Rejected catalog request reached upstream')
    async with AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        monkeypatch.setattr(app.state, 'http', http)
        r = await clients.get(f'/call/{PROFILE}?{query}')
    assert r.status_code == 400, r.text
    assert 'private' not in r.text
    assert await balance(clients) == before


@pytest.mark.parametrize('params', [{}, {'main': 'false'}, {'main': 'true', 'findEmail': 'true'}])
async def test_harvest_basic_variant_requires_exact_mode(clients, harvest_on, params):
    before = await balance(clients)
    r = await clients.get(f'/call/{PROFILE}.main', params={'publicIdentifier': 'example', **params})
    assert r.status_code == 400
    assert await balance(clients) == before


async def test_harvest_get_body_is_rejected(clients, harvest_on):
    before = await balance(clients)
    r = await clients.request('GET', f'/call/{PROFILE}?publicIdentifier=example', json={'cookie': 'private'})
    assert r.status_code == 400
    assert await balance(clients) == before


@pytest.mark.parametrize('body,charge', [
    ({'element': {'id': 'example'}, 'status': 200, 'cost': 0.0064}, 6400),
    ({'element': None, 'status': 404, 'error': 'Profile not found', 'cost': 0.004}, 4000),
    ({'element': None, 'status': 400, 'cost': 0}, 0),
    ({'elements': [], 'status': 200, 'cost': 0.004}, 4000),
    ({'element': {}, 'cost': None}, 6400),
    ({'element': {}, 'cost': -1}, 6400),
    ({'element': {}, 'cost': 'NaN'}, 6400),
    ({'element': {}, 'cost': True}, 6400),
    ({'element': {}}, 6400),
])
async def test_harvest_settles_reported_cost_including_billed_misses(clients, monkeypatch, harvest_on, body, charge):
    def serve(request):
        assert request.headers['x-api-key'] == 'TEST-PLATFORM-HARVEST'
        assert request.url.path == '/linkedin/profile'
        return response(200, json=body)
    before = await balance(clients)
    async with AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        r = await clients.get(f'/call/{PROFILE}?publicIdentifier=example')
    assert r.status_code == 200, r.text
    assert r.json() == body
    assert r.headers['x-treg-cost-micro'] == str(charge)
    after = await balance(clients)
    assert after['balance_micro'] == before['balance_micro'] - charge
    entries = after['entries']['items']
    assert sum(e['kind'] == 'reserve' for e in entries) == 1
    assert sum(e['kind'] in {'settle', 'release'} for e in entries) == 1


@pytest.mark.parametrize('suffix,params,charge', [
    ('.main', {'publicIdentifier': 'example', 'main': 'true'}, 4000),
    ('.email', {'publicIdentifier': 'example', 'findEmail': 'true'}, 20000),
])
async def test_harvest_variants_reserve_their_own_price(clients, monkeypatch, harvest_on, suffix, params, charge):
    async with AsyncClient(transport=httpx.MockTransport(lambda r: response(200, json={'element': {}, 'cost': charge / 1_000_000}))) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        r = await clients.get('/call/' + PROFILE + suffix, params=params)
    assert r.status_code == 200, r.text
    assert r.headers['x-treg-cost-micro'] == str(charge)
    entries = (await balance(clients))['entries']['items']
    reserve = next(e for e in entries if e['kind'] == 'reserve')
    assert abs(reserve['amount_micro']) == charge


@pytest.mark.parametrize('status', [401, 429, 500])
async def test_harvest_upstream_failure_releases_hold(clients, monkeypatch, harvest_on, status):
    before = await balance(clients)
    async with AsyncClient(transport=httpx.MockTransport(lambda r: response(status, json={'error': 'failed'}))) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        r = await clients.get(f'/call/{PROFILE}?publicIdentifier=example')
    assert r.status_code == status
    after = await balance(clients)
    assert after['balance_micro'] == before['balance_micro']
    assert [e['kind'] for e in after['entries']['items']][:2] == ['release', 'reserve']


async def test_harvest_own_key_wins_without_metering_even_when_provider_rejects(clients, monkeypatch, harvest_on):
    await clients.post('/secrets', json={'name': 'harvestapi', 'value': 'TEST-OWN'})
    seen = []
    def serve(request):
        seen.append(request.headers['x-api-key'])
        return response(401, json={'error': 'Invalid own key'})
    before = await balance(clients)
    async with AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        r = await clients.get(f'/call/{PROFILE}?publicIdentifier=example')
    assert r.status_code == 401
    assert seen == ['TEST-OWN']
    assert 'x-treg-cost-micro' not in r.headers
    assert await balance(clients) == before


async def test_harvest_no_key_no_platform_rejects(clients):
    before = await balance(clients)
    r = await clients.get(f'/call/{PROFILE}?publicIdentifier=example')
    assert r.status_code == 404, r.text
    assert await balance(clients) == before


@pytest.mark.parametrize('routed', [False, True])
@pytest.mark.parametrize('own', [False, True])
async def test_harvest_cost_ceiling_blocks_platform_but_not_own_key(
        clients, monkeypatch, harvest_on, routed, own):
    if own:
        await clients.post('/secrets', json={'name': 'harvestapi', 'value': 'TEST-OWN'})
    seen = []
    def serve(request):
        seen.append(request.headers['x-api-key'])
        return response(200, json={'element': {'id': '1', 'name': 'Example'},
                                   'status': 200, 'cost': 0.004})
    before = await balance(clients)
    async with AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        headers = {'X-Treg-Route-Max-Cost': '0'}
        if routed:
            result = await clients.post('/call/treg.linkedin.company.profile',
                                        json={'linkedin_handle': 'example'}, headers=headers)
        else:
            result = await clients.get('/call/' + COMPANY,
                                       params={'universalName': 'example'}, headers=headers)
    assert result.status_code == (200 if own else 402), result.text
    assert seen == (['TEST-OWN'] if own else [])
    assert await balance(clients) == before
    if not own:
        assert result.json()['detail']['error'] == 'route_max_cost'


@pytest.mark.parametrize('value,expected', [(0, 0), (19.75, 19.75), (None, None), (-1, None), ('20', None), (True, None)])
async def test_harvest_balance_reads_remaining_not_total(value, expected):
    def serve(request):
        assert request.headers['x-api-key'] == 'test-key'
        return response(200, json={'usage': {'balance': value}, 'user': {'totalBalance': 999}})
    async with AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        row = await collectors._harvestapi(upstream, 'test-key')
    assert row['value'] == expected
    assert row['unit'] == 'USD'


def test_harvest_auto_topup_policy_still_respects_empty_balance():
    from treg.domain.capacity.policy import default_policy, latest_state
    from treg.models import CapacitySnapshot
    from treg.timeutil import utcnow_naive

    policy = default_policy('harvestapi', has_key=True)
    assert policy.capacity_type == 'cash'
    assert policy.funding_mode == 'auto_recharge'
    assert policy.auto_funding_enabled
    now = utcnow_naive()
    empty = CapacitySnapshot(provider='harvestapi', observed_at=now, remaining=0, unit='USD')
    assert latest_state(policy, empty, now).is_exhausted(now)
    funded = CapacitySnapshot(provider='harvestapi', observed_at=now, remaining=20, unit='USD')
    assert not latest_state(policy, funded, now).is_exhausted(now)


@pytest.mark.parametrize('capability,identity,child,path,query,element,charge,field', [
    ('people.enrich', {'linkedin_url': 'https://www.linkedin.com/in/example'}, PROFILE, '/linkedin/profile',
     {'publicIdentifier': 'example'}, {'id': '1', 'firstName': 'Alex', 'lastName': 'Example'}, 6400, 'full_name'),
    ('companies.enrich', {'linkedin_url': 'https://www.linkedin.com/company/example'}, COMPANY, '/linkedin/company',
     {'universalName': 'example'}, {'id': '1', 'name': 'Example'}, 4000, 'name'),
    ('linkedin.user.profile', {'linkedin_handle': 'example'}, PROFILE + '.main', '/linkedin/profile',
     {'publicIdentifier': 'example', 'main': 'true'}, {'id': '1', 'firstName': 'Alex', 'lastName': 'Example'}, 4000, 'full_name'),
    ('linkedin.company.profile', {'linkedin_handle': 'example'}, COMPANY, '/linkedin/company',
     {'universalName': 'example'}, {'id': '1', 'name': 'Example'}, 4000, 'name'),
    ('people.email.find', {'linkedin_handle': 'example'}, PROFILE + '.email', '/linkedin/profile',
     {'publicIdentifier': 'example', 'findEmail': 'true'},
     {'id': '1', 'emails': [{'email': 'alex@example.com', 'status': 'valid', 'qualityScore': 80}]}, 20000, 'email'),
    ('linkedin.post.detail', {'url': 'https://www.linkedin.com/posts/example'},
     'harvestapi.linkedin.post.get', '/linkedin/post', {'url': 'https://www.linkedin.com/posts/example'},
     {'id': '1', 'content': 'Example post'}, 4000, 'post'),
])
async def test_harvest_routed_contracts_preserve_cost_and_disclose_child(
        clients, monkeypatch, harvest_on, capability, identity, child, path, query, element, charge, field):
    raw = {'element': element, 'status': 200, 'cost': charge / 1_000_000}
    def serve(request):
        assert request.method == 'GET' and request.url.path == path
        assert dict(request.url.params) == query
        assert request.headers['x-api-key'] == 'TEST-PLATFORM-HARVEST'
        return response(200, json=raw)
    before = (await balance(clients))['balance_micro']
    async with AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        r = await clients.post('/call/treg.' + capability, json=identity)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result['_treg']['served_by'] == child
    assert result['output'][field]
    assert result['raw'] == raw
    assert before - (await balance(clients))['balance_micro'] == charge


def test_harvest_arena_categories_preserve_native_routes():
    from treg.application.arena import public_tasks
    cat = store.load()
    expected = {'people.enrich': PROFILE, 'companies.enrich': COMPANY,
                'people.email.find': PROFILE + '.email'}
    for task in public_tasks():
        for variant, previews in zip(task['variants'], task['provider_previews']):
            rows = [p for p in previews if p['provider'] == 'harvestapi']
            if task['id'] in expected and variant == ('linkedin_url',):
                assert [p['endpoint_id'] for p in rows] == [expected[task['id']]]
            else:
                assert not rows
    assert PROFILE in {e['id'] for e in cat.for_capability('linkedin.user.profile')}
    assert COMPANY in {e['id'] for e in cat.for_capability('linkedin.company.profile')}
    assert PROFILE + '.main' not in {e['id'] for e in cat.for_capability('people.enrich')}


@pytest.mark.parametrize('mode', ['compare', 'waterfall'])
@pytest.mark.parametrize('own', [False, True])
@pytest.mark.parametrize('capability,identity,path,query,element,charge', [
    ('people.enrich', {'linkedin_url': 'https://www.linkedin.com/in/example'},
     '/linkedin/profile', {'publicIdentifier': 'example'},
     {'id': '1', 'firstName': 'Alex', 'lastName': 'Example'}, 6400),
    ('companies.enrich', {'linkedin_url': 'https://www.linkedin.com/company/example'},
     '/linkedin/company', {'universalName': 'example'}, {'id': '1', 'name': 'Example'}, 4000),
])
async def test_harvest_arena_modes_use_normal_credentials_and_billing(
        clients, monkeypatch, harvest_on, mode, own, capability, identity, path, query, element, charge):
    from test_enrich_arena import finish
    if own:
        await clients.post('/secrets', json={'name': 'harvestapi', 'value': 'TEST-OWN'})
    seen = []
    def serve(request):
        seen.append(request)
        assert request.url.path == path and dict(request.url.params) == query
        assert request.headers['x-api-key'] == ('TEST-OWN' if own else 'TEST-PLATFORM-HARVEST')
        return response(200, json={'element': element, 'cost': charge / 1_000_000})
    before = (await balance(clients))['balance_micro']
    async with AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        q = await clients.post('/arena/plans', json={'capability': capability, 'identity': identity,
            'mode': mode, 'providers': ['harvestapi'], 'max_cost_micro': 1_000_000})
        assert q.status_code == 200, q.text
        assert not seen
        result = await finish(clients, q.json())
    assert len(seen) == 1
    assert result['results'][0]['state'] == 'hit'
    assert before - (await balance(clients))['balance_micro'] == (0 if own else charge)
