"""The proposed Influship billing metadata settles actual charges against team balances."""
import json

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A, oauth_providers
from treg.domain.catalog import store as catalog_store
from treg.config import get_settings


@pytest.fixture
def influship_platform(monkeypatch):
    monkeypatch.setenv('TREG_PLATFORM_KEY_INFLUSHIP', 'TEST-INFLUSHIP-PLATFORM-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'influship')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def balance(client):
    org_id = (await client.get('/orgs')).json()[0]['org_id']
    return (await client.get(f'/orgs/{org_id}/balance')).json()['balance_micro']


@pytest.mark.parametrize('endpoint,body,rows,credits,micro', [
    ('influship.searchcreators', {'query': 'science', 'limit': 5}, 0, '25.00', 250_000),
    ('influship.searchcreators', {'query': 'science', 'limit': 1}, 1, '27.00', 270_000),
    ('influship.searchcreators', {'query': 'science', 'limit': 5}, 5, '35.00', 350_000),
    ('influship.lookupprofiles', {'profiles': [
        {'platform': 'instagram', 'username': 'nasa'},
        {'platform': 'instagram', 'username': 'spacex'}]}, 1, '0.10', 1_000),
    ('influship.findlookalikecreators', {'seeds': [
        {'platform': 'instagram', 'username': 'spacex'}], 'limit': 1}, 0, '0.00', 0),
])
async def test_platform_balances_follow_header_not_reserved_results(
    clients, monkeypatch, influship_platform, endpoint, body, rows, credits, micro,
):
    def upstream(request):
        assert request.url.host == 'api.influship.com'
        assert request.headers['x-api-key'] == 'TEST-INFLUSHIP-PLATFORM-KEY'
        assert json.loads(request.content) == body
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps({
            'data': [{'id': str(i)} for i in range(rows)]}).encode()),
            headers={'Content-Type': 'application/json', 'X-Credits-Charged': credits})

    async with AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(A.app.state, 'http', client)
        before = await balance(clients)
        response = await clients.post('/call/' + endpoint, json=body)
        assert response.status_code == 200, response.text
        assert int(response.headers['x-treg-cost-micro']) == micro
        assert await balance(clients) == before - micro


async def test_invalid_search_count_never_calls_upstream(
    clients, monkeypatch, influship_platform,
):
    def unexpected(request):
        pytest.fail('Invalid count must be refused before an upstream call')

    async with AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        monkeypatch.setattr(A.app.state, 'http', client)
        before = await balance(clients)
        response = await clients.post('/call/influship.searchcreators',
                                      json={'query': 'science', 'limit': 101})
        assert response.status_code in (400, 422), response.text
        assert await balance(clients) == before


def test_every_influship_core_endpoint_has_computable_platform_pricing():
    catalog = catalog_store.load()
    entries = [ep for ep in catalog.by_id.values() if ep['provider'] == 'influship']
    assert entries
    assert all(catalog.platform_eligible(ep) for ep in entries)


def test_platform_key_preserves_the_byok_header_shape():
    assert oauth_providers.platform_bindings(oauth_providers.INFLUSHIP) == [{
        'platform_setting': 'platform_key_influship', 'injector': 'env',
        'location': 'header', 'name': 'X-API-Key', 'format': '{secret}',
    }]
