"""Harvest's API-key-only catalog, shared opt-in constraints and reported-charge settlement."""

import httpx
import pytest
from httpx import AsyncClient

from treg.api import app
from treg.config import get_settings
from treg.domain.capacity import collectors

PROFILE = 'harvestapi.linkedin.user.profile'


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


@pytest.mark.parametrize('value,expected', [(0, 0), (19.75, 19.75), (None, None), (-1, None), ('20', None), (True, None)])
async def test_harvest_balance_reads_remaining_not_total(value, expected):
    def serve(request):
        assert request.headers['x-api-key'] == 'test-key'
        return response(200, json={'usage': {'balance': value}, 'user': {'totalBalance': 999}})
    async with AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        row = await collectors._harvestapi(upstream, 'test-key')
    assert row['value'] == expected
    assert row['unit'] == 'USD'
