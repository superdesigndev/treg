"""Synthetic regressions: a work address must answer the requested identity."""

import pytest

from treg.application.call import service as call_service
from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import enrichment_on, _relay_by_provider  # noqa: F401

ROUTE = '/call/treg.people.email.find'
PERSON = {'full_name': 'Avery van der Example', 'first_name': 'Avery',
          'last_name': 'van der Example', 'domain': 'employer.test'}


def _tomba(**fields):
    return {'data': {'email': 'avery@employer.test', 'first_name': 'Avery',
                     'last_name': 'van der Example', 'score': 90, **fields}}


@pytest.mark.parametrize('fields,reason', [
    ({'email': 'avery@other-employer.test'}, 'email_domain_mismatch'),
    ({'first_name': 'Someone'}, 'first_name_mismatch'),
    ({'last_name': 'Example'}, 'last_name_mismatch'),
])
async def test_contradictory_provider_answer_is_not_the_route_winner(clients, enrichment_on, monkeypatch, fields, reason):
    seen = []
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({
        'tomba': [(200, _tomba(**fields))],
        'hunter': [(200, {'data': {'email': 'avery@employer.test', 'first_name': 'Avery',
                                  'last_name': 'van der Example', 'score': 90}})],
    }, seen))
    before = await _balance(clients)
    result = await clients.post(ROUTE, json=PERSON,
                               headers={'X-Treg-Route-Prefer': 'tomba,hunter', 'X-Treg-Route-Max-Cost': '0.08'})
    assert result.status_code == 200, result.text
    doc = result.json()
    assert doc['_treg']['served_by'] == 'hunter.people.email.find'
    assert doc['output']['email'] == 'avery@employer.test'
    assert doc['_treg']['tried'][0]['outcome'] == 'error'
    assert doc['_treg']['tried'][0]['detail'] == reason
    assert [provider for provider, *_ in seen] == ['tomba', 'hunter']
    assert before - await _balance(clients) == int(result.headers['X-Treg-Cost-Micro']) == 33_400


async def test_identity_failure_is_not_a_miss_and_cannot_exceed_the_remaining_budget(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({
        'tomba': [(200, _tomba(email='avery@other-employer.test'))],
    }, seen))
    before = await _balance(clients)
    result = await clients.post(ROUTE, json=PERSON,
                               headers={'X-Treg-Route-Prefer': 'tomba', 'X-Treg-Route-Max-Cost': '0.009'})
    assert result.status_code == 502, result.text
    assert result.json()['detail']['error'] == 'route_failed'
    # Existing unsuccessful-route settlement releases its child holds. The
    # paid estimate still prevented another provider from exceeding the cap.
    assert result.json()['detail']['charged_micro'] == 0
    assert before - await _balance(clients) == 0
    assert len(seen) == 1






async def test_full_name_only_does_not_guess_middle_names_into_tomba_surname(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({'tomba': [(200, _tomba(last_name='Example'))]}, seen))
    result = await clients.post(ROUTE, json={'full_name': 'Avery Jordan Example', 'domain': 'employer.test'},
                               headers={'X-Treg-Route-Prefer': 'tomba'})
    assert result.status_code == 200, result.text
    assert seen[0][2] == {'domain': 'employer.test', 'full_name': 'Avery Jordan Example'}




def test_email_adapter_prefers_actual_input_fields_over_derived_name_parts():
    from treg.domain.catalog import store as catalog_store
    from treg.domain.catalog.routing.contracts import Adapter, canonical_identity
    from treg.domain.catalog.routing.plan import candidates_for

    contract = catalog_store.load().contracts['people.email.find']
    adapter = Adapter(endpoint_id='synthetic.email.find',
                      accepts=(('domain', 'first_name', 'last_name'), ('domain', 'full_name')),
                      in_map={}, out_map={}, miss='', verified=True)
    endpoint = {'id': adapter.endpoint_id, 'provider': 'synthetic'}
    whole = {'full_name': 'Avery Jordan Example', 'domain': 'employer.test'}
    for given, expected in [(whole, ('domain', 'full_name')),
                            (PERSON, ('domain', 'first_name', 'last_name'))]:
        identity, _ = canonical_identity(contract, given)
        candidates, dropped = candidates_for(contract, [endpoint], {adapter.endpoint_id: adapter},
                                             identity, given=set(given))
        assert not dropped
        assert candidates[0][2] == expected
