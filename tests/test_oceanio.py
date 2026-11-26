"""Ocean.io on treg's key: the reported credits settle, the search size and reveal bounds refuse
before any hold, and a team's own key keeps Ocean.io's own range unmetered."""
import json

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A
from treg.config import get_settings
from treg.domain import money
from treg.infra.db import session_maker
from test_marketplace_call import _balance, _dropleads_response, _entries

SEARCHES = ('oceanio.companies.lookalike', 'oceanio.companies.search', 'oceanio.people.search')
BASE = {'oceanio.companies.lookalike': {'companiesFilters': {'lookalikeDomains': ['example.com']}}}


@pytest.fixture
def oceanio_on(monkeypatch):
    monkeypatch.setenv('TREG_PLATFORM_KEY_OCEANIO', 'PLATFORM-OCEANIO')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'oceanio')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _call(clients, monkeypatch, endpoint, body, answer, seen=None):
    def upstream(request):
        if seen is not None:
            seen.append(request)
        status, doc = answer
        return _dropleads_response(status, doc)

    async with AsyncClient(transport=httpx.MockTransport(upstream)) as vendor:
        monkeypatch.setattr(A.app.state, 'http', vendor)
        return await clients.post(f'/call/{endpoint}', json=body)


@pytest.mark.parametrize('endpoint', SEARCHES)
@pytest.mark.parametrize('size', [1, 100])
async def test_search_holds_the_size_and_settles_the_reported_credits(clients, oceanio_on, monkeypatch, endpoint, size):
    org_id = (await clients.get('/orgs')).json()[0]['org_id']
    async with session_maker() as db:
        await money.grant(db, org_id, amount_micro=5_000_000, kind='promotional', once=False)
        await db.commit()
    seen = []
    before = await _balance(clients)
    r = await _call(clients, monkeypatch, endpoint, {**BASE.get(endpoint, {}), 'size': size}, (200, {'creditsUsed': 0.2, 'companies': [{}]}), seen)
    assert r.status_code == 200, r.text
    assert seen[0].headers['x-api-token'] == 'PLATFORM-OCEANIO'
    reserves = [e for e in await _entries(clients) if e['kind'] == 'reserve']
    assert [-e['amount_micro'] for e in reserves] == [money.with_margin(size * 16_200)]
    assert before - await _balance(clients) == money.with_margin(16_200)


@pytest.mark.parametrize('endpoint', SEARCHES)
@pytest.mark.parametrize('body', [
    {}, {'size': None}, {'size': 0}, {'size': 101}, {'size': 10_000}, {'size': 10_001},
    {'size': True}, {'size': '10'}, {'size': 1.5},
])
async def test_search_refuses_a_size_outside_1_to_100_before_any_hold(clients, oceanio_on, monkeypatch, endpoint, body):
    seen = []
    before = await _balance(clients)
    r = await _call(clients, monkeypatch, endpoint, {**BASE.get(endpoint, {}), **body}, (200, {'creditsUsed': 0.2}), seen)
    assert r.status_code == 400, r.text
    assert r.json()['detail']['parameter'] == 'body.size'
    assert not seen
    assert await _balance(clients) == before
    assert not [e for e in await _entries(clients) if e['kind'] in ('reserve', 'settle', 'release')]


@pytest.mark.parametrize('field,value', [
    ('revealEmails', {'includeEmails': True, 'webhookUrl': 'https://example.com/h'}),
    ('revealPhones', {'includePhones': True, 'webhookUrl': 'https://example.com/h'}),
    ('revealEmails', None),
    ('revealPhones', {}),
])
async def test_person_enrich_refuses_the_separately_billed_reveals(clients, oceanio_on, monkeypatch, field, value):
    seen = []
    before = await _balance(clients)
    body = {'person': {'linkedin': 'https://www.linkedin.com/in/example'}, field: value}
    r = await _call(clients, monkeypatch, 'oceanio.people.enrich', body, (200, {'creditsUsed': 0.1}), seen)
    assert r.status_code == 400, r.text
    assert r.json()['detail']['parameter'] == f'body.{field}'
    assert not seen
    assert await _balance(clients) == before


@pytest.mark.parametrize('answer,charged', [
    ((200, {'name': 'A B', 'creditsUsed': 0.1}), 8_100),
    ((201, {'detail': 'Data gathering process has started. Please try again later.'}), 0),
    ((200, {'name': 'A B'}), 0),
    ((404, {'detail': 'No matching person found'}), 0),
])
async def test_person_enrich_settles_the_reported_credits_and_zero_without_them(clients, oceanio_on, monkeypatch, answer, charged):
    before = await _balance(clients)
    r = await _call(clients, monkeypatch, 'oceanio.people.enrich', {'person': {'linkedin': 'https://www.linkedin.com/in/example'}}, answer)
    assert r.status_code == answer[0], r.text
    assert before - await _balance(clients) == money.with_margin(charged)


async def test_an_empty_search_is_free(clients, oceanio_on, monkeypatch):
    before = await _balance(clients)
    r = await _call(clients, monkeypatch, 'oceanio.companies.search', {'size': 2},
                    (200, {'detail': 'OK', 'total': 0, 'creditsUsed': 0.0, 'companies': []}))
    assert r.status_code == 200, r.text
    assert await _balance(clients) == before


@pytest.mark.parametrize('body,status', [
    ({'size': 101}, 200), ({'size': 10_000}, 200), ({}, 200), ({'size': 10_001}, 400),
])
async def test_own_key_keeps_ocean_io_range_and_is_unmetered(clients, oceanio_on, monkeypatch, body, status):
    saved = await clients.post('/secrets', json={'name': 'oceanio', 'value': 'OWN-OCEANIO'})
    assert saved.status_code == 200, saved.text
    seen = []
    before = await _balance(clients)
    r = await _call(clients, monkeypatch, 'oceanio.companies.search', body, (200, {'creditsUsed': 20.2}), seen)
    assert r.status_code == status, r.text
    if status == 200:
        assert seen[0].headers['x-api-token'] == 'OWN-OCEANIO'
        assert json.loads(seen[0].content) == body
        assert 'x-treg-cost-micro' not in r.headers
    else:
        assert not seen
    assert await _balance(clients) == before


async def test_own_key_sends_the_reveals(clients, oceanio_on, monkeypatch):
    await clients.post('/secrets', json={'name': 'oceanio', 'value': 'OWN-OCEANIO'})
    seen = []
    body = {'person': {'linkedin': 'https://www.linkedin.com/in/example'},
            'revealPhones': {'includePhones': True, 'webhookUrl': 'https://example.com/h'}}
    r = await _call(clients, monkeypatch, 'oceanio.people.enrich', body, (200, {'creditsUsed': 0.1}), seen)
    assert r.status_code == 200, r.text
    assert json.loads(seen[0].content) == body
