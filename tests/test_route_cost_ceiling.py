"""Caller ceilings are enforced against reservations, not advisory route quotes."""

from dataclasses import replace

import pytest
from sqlmodel import select

from treg.application.call import route as routing
from treg.application.call import service
from treg.config import get_settings
from treg.domain import money
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _relay_by_provider, enrichment_on  # noqa: F401


@pytest.fixture
def moz_on(monkeypatch, platform_on):
    monkeypatch.setenv('TREG_PLATFORM_KEY_MOZ', 'FAKE-MOZ-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'moz')
    get_settings.cache_clear()


def _rows(count):
    return {'results': [{'source': {'page': f'https://source.example/{i}'},
                         'target': {'page': 'https://target.example/'}} for i in range(count)]}


async def _assert_no_holds():
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


@pytest.mark.parametrize('endpoint', ['moz.web.backlinks.list', 'treg.web.backlinks.list'])
@pytest.mark.parametrize('cap,margin,status', [
    ('0.01', '0', 402), ('0.333349', '0', 402), ('0.333350', '0', 200),
    ('0.333350', '0.2', 402), ('0.400020', '0.2', 200),
])
async def test_backlinks_ceiling(clients, moz_on, monkeypatch, endpoint, cap, margin, status):
    monkeypatch.setenv('TREG_PLATFORM_MARGIN', margin)
    get_settings.cache_clear()
    seen = []
    raw = _rows(50)
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'moz': [(200, raw)]}, seen))
    before = await _balance(clients)
    response = await clients.post('/call/' + endpoint,
        json={'target': 'https://target.example/', 'limit': 50},
        headers={'X-Treg-Route-Max-Cost': cap, 'X-Treg-Route-Waterfall': '0'})
    assert response.status_code == status, response.text
    charged = before - await _balance(clients)
    if status == 402:
        assert response.json()['detail']['error'] == 'route_max_cost'
        assert charged == 0 and seen == []
        async with session_maker() as db:
            assert (await db.execute(select(LedgerEntry).where(
                LedgerEntry.kind.in_(['reserve', 'settle', 'release'])))).scalars().all() == []
    else:
        assert charged == money.with_margin(333350)
        assert int(response.headers['x-treg-cost-micro']) == charged
        assert len(seen) == 1 and seen[0][3]['limit'] == 50
        assert (response.json()['raw'] if endpoint.startswith('treg.') else response.json()) == raw
    await _assert_no_holds()


async def test_underquoted_candidate_is_refused_at_reservation(clients, moz_on, monkeypatch):
    monkeypatch.setattr(routing, 'cost_at', lambda *args: 1)
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'moz': [(200, _rows(50))]}, seen))
    before = await _balance(clients)
    response = await clients.post('/call/treg.web.backlinks.list',
        json={'target': 'https://target.example/', 'limit': 50},
        headers={'X-Treg-Route-Max-Cost': '0.01'})
    assert response.status_code == 402, response.text
    assert response.json()['detail']['error'] == 'route_max_cost'
    assert seen == [] and await _balance(clients) == before
    await _assert_no_holds()


async def test_default_ceiling_also_reaches_reservation(clients, moz_on, monkeypatch):
    monkeypatch.setattr(routing, 'cost_at', lambda *args: 1)
    monkeypatch.setenv('TREG_PLATFORM_MARGIN', '3')
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'moz': [(200, _rows(50))]}, seen))
    response = await clients.post('/call/treg.web.backlinks.list',
        json={'target': 'https://target.example/', 'limit': 50})
    assert response.status_code == 402, response.text
    assert seen == []
    await _assert_no_holds()


async def test_paid_attempt_consumes_remaining_ceiling(clients, moz_on, monkeypatch):
    build = routing.build_plan
    async def repeated_candidates(*args, **kwargs):
        plan = await build(*args, **kwargs)
        plan.candidates = [replace(plan.candidates[0], price_micro=1) for _ in range(4)]
        return plan
    monkeypatch.setattr(routing, 'build_plan', repeated_candidates)
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'moz': [(200, _rows(1))] * 4}, seen))
    before = await _balance(clients)
    response = await clients.post('/call/treg.web.backlinks.list',
        json={'target': 'https://target.example/', 'limit': 1},
        headers={'X-Treg-Route-Max-Cost': '0.01', 'X-Treg-Route-Min-Results': '2'})
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert before - await _balance(clients) == 6667
    assert [t['outcome'] for t in response.json()['_treg']['tried']] == ['weak', 'skipped', 'skipped', 'skipped']
    assert response.json()['raw'] == _rows(1)
    assert response.json()['_treg']['outcome'] == 'weak'
    assert response.headers['x-treg-route-outcome'] == 'weak'
    await _assert_no_holds()


_EMAIL = 'treg.people.email.find'
_DEAR = 'hunter.people.email.find'
_CHEAP = 'tomba.people.email.find'
_IDENTITY = {'full_name': 'Treg Fixture', 'domain': 'example.com'}


def _pin_prices(plan, order):
    """Place `order` of (endpoint id, price_micro) first; every other candidate is over any test ceiling."""
    by_id = {c.endpoint['id']: c for c in plan.candidates}
    missing = [eid for eid, _ in order if eid not in by_id]
    assert not missing, sorted(by_id)
    pinned = {eid for eid, _ in order}
    plan.candidates = [replace(by_id[eid], price_micro=price) for eid, price in order] + [
        replace(c, price_micro=1_000_000) for c in plan.candidates if c.endpoint['id'] not in pinned]
    return plan


async def test_ceiling_tries_a_later_affordable_candidate(clients, enrichment_on, monkeypatch):
    """Specificity can rank a dear candidate first. The ceiling must not 402 while a later one fits."""
    build = routing.build_plan

    async def expensive_first(*args, **kwargs):
        plan = await build(*args, **kwargs)
        return _pin_prices(plan, [(_DEAR, 245_000), (_CHEAP, 54_000)])

    monkeypatch.setattr(routing, 'build_plan', expensive_first)
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({
        'hunter': [(200, {'data': {'email': 'skipped@example.com', 'score': 90,
                                   'verification': {'status': 'valid'}}})],
        'tomba': [(200, {'data': {'email': 'found@example.com', 'score': 90,
                                  'verification': {'status': 'valid'}}})],
    }, seen))
    response = await clients.post('/call/' + _EMAIL, json=_IDENTITY,
                                  headers={'X-Treg-Route-Max-Cost': '0.054'})
    assert response.status_code == 200, response.text
    tried = response.json()['_treg']['tried']
    assert [t['endpoint_id'] for t in tried] == [_DEAR, _CHEAP]
    assert tried[0]['outcome'] == 'skipped' and 'max cost' in tried[0]['detail']
    assert tried[1]['outcome'] == 'hit'
    assert response.json()['_treg']['served_by'] == _CHEAP
    assert [provider for provider, *_ in seen] == ['tomba']
    await _assert_no_holds()


async def test_ceiling_names_the_minimum_when_nothing_fits(clients, enrichment_on, monkeypatch):
    build = routing.build_plan

    async def expensive_first(*args, **kwargs):
        plan = await build(*args, **kwargs)
        return _pin_prices(plan, [(_DEAR, 245_000), (_CHEAP, 54_000)])

    monkeypatch.setattr(routing, 'build_plan', expensive_first)
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba': [(200, {})], 'hunter': [(200, {})]}, seen))
    before = await _balance(clients)
    response = await clients.post('/call/' + _EMAIL, json=_IDENTITY,
                                  headers={'X-Treg-Route-Max-Cost': '0.01'})
    assert response.status_code == 402, response.text
    detail = response.json()['detail']
    assert detail['error'] == 'route_max_cost'
    assert detail['endpoint_id'] == _EMAIL
    assert detail['max_cost_micro'] == 10_000
    assert detail['cheapest_micro'] == 54_000
    assert detail['charged_micro'] == 0
    assert detail['plan'][0]['endpoint_id'] == _DEAR and detail['plan'][0]['price_micro'] == 245_000
    assert detail['plan'][1]['endpoint_id'] == _CHEAP
    assert detail['message'] == (
        'the cheapest candidate (tomba.people.email.find) costs more than x-treg-route-max-cost')
    assert seen == [] and await _balance(clients) == before
    await _assert_no_holds()


async def test_own_key_remains_unmetered_with_zero_ceiling(clients, moz_on, monkeypatch):
    response = await clients.post('/secrets', json={'name': 'moz', 'value': 'OWN-FAKE-MOZ-KEY'})
    assert response.status_code < 300, response.text
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'moz': [(200, _rows(50))]}, seen))
    before = await _balance(clients)
    response = await clients.post('/call/treg.web.backlinks.list',
        json={'target': 'https://target.example/', 'limit': 50},
        headers={'X-Treg-Route-Max-Cost': '0'})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and await _balance(clients) == before
    assert response.json()['_treg']['tier'] == 'credential'
    await _assert_no_holds()
