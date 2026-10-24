"""Generic metered prices: bounded holds and exact header settlement."""
import json

import httpx
import pytest

from scripts import catalog_validate as validator
from treg.application.call import resolve, settle
from treg.application.call.types import ResolutionFailed
from treg.domain.catalog import store


@pytest.mark.parametrize('body,expected', [({}, 750000), ({'limit': 1}, 270000),
    ({'limit': 5}, 350000), ({'limit': 100}, 2250000)])
def test_linear_hold_includes_base_and_documented_default(body, expected):
    cost = {'type': 'per_result', 'usd': .02, 'base_usd': .25,
            'request_count': {'location': 'body', 'field': 'limit', 'mode': 'value',
                              'default': 25, 'maximum': 100}}
    assert resolve._platform_estimate_micro(cost, {}, json.dumps(body).encode()) == expected


@pytest.mark.parametrize('bad', [0, 101, -1, True, 'wrong', '²', [], 1.5])
def test_invalid_count_is_refused_before_upstream(bad):
    cost = {'type': 'per_result', 'usd': .02,
            'request_count': {'location': 'body', 'field': 'limit', 'mode': 'value',
                              'default': 25, 'maximum': 100}}
    with pytest.raises(ResolutionFailed):
        resolve._platform_estimate_micro(cost, {}, json.dumps({'limit': bad}).encode())


def test_input_array_reserves_each_requested_item():
    cost = {'type': 'per_result', 'usd': .001,
            'request_count': {'location': 'body', 'field': 'profiles', 'mode': 'length',
                              'default': 100, 'maximum': 100}}
    assert resolve._platform_estimate_micro(cost, {}, b'{"profiles":[{}, {}, {}]}') == 3000
    assert resolve._platform_estimate_micro(cost, {}, b'{}') == 100000


@pytest.mark.parametrize('unit,rate,amount,expected', [
    ('credit', 10000, '35.00', 350000), ('credit', 10000, '0', 0),
    ('usd', 0, '0.0000015', 2), ('usd', 0, 'NaN', None),
    ('credit', 10000, '-1', None), ('credit', 10000, 'bad', None),
    ('credit', 10000, None, None), ('credit', 10000, 'Infinity', None),
])
def test_catalog_header_meter_is_generic_and_reads_before_body(monkeypatch, unit, rate, amount, expected):
    ep = {'id': 'example.search', 'provider': 'example', 'cost': {
        'type': 'per_result', 'value': .02, 'currency': 'USD',
        'reported_charge': {'header': 'X-Actual-Charge', 'unit': unit}}}
    monkeypatch.setitem(store.load().by_id, ep['id'], ep)
    mk = resolve.MarketplaceCall(tool=None, upstream='', consumed=set(), provider='example',
        tier='platform', endpoint_id=ep['id'], reported_charge_unit_micro=rate)
    headers = httpx.Headers({'x-actual-charge': amount}) if amount is not None else httpx.Headers()
    assert settle._observed_cost_micro(mk, b'', headers) == expected


def test_base_conversion_preserves_per_result_denomination():
    view = store.load().cost_view({'type': 'per_result', 'value': 2, 'base_value': 25,
                                  'currency': 'USD', 'per': 100}, 'example')
    assert view['usd'] == .02
    assert view['base_usd'] == 25


@pytest.mark.parametrize('rule', [None, [], {'header':'X-Meter','path':'meter','unit':'usd'},
                                 {'header':'X-Meter\nInjected','unit':'usd'},
                                 {'header':'','unit':'usd'}, {'header':'X-Meter','unit':'usd'}])
def test_header_descriptor_rejects_ambiguous_or_unsafe_shapes(rule):
    cost = dict(store.load().by_id['trykitt.people.email.find']['cost'])
    cost['reported_charge'] = rule
    errors = []
    validator.check_cost(cost, 'test', errors, [], provider='serper')
    assert bool(errors) is (rule not in (None, {'header':'X-Meter','unit':'usd'}))


def test_count_rule_must_reference_a_declared_typed_field():
    cost = dict(store.load().by_id['trykitt.people.email.find']['cost'])
    cost.update(type='per_result', base_value=.25,
        reported_charge={'header':'X-Meter','unit':'usd'},
        request_count={'location':'body','field':'limit','mode':'value','default':25,'maximum':100})
    errors=[]
    validator.check_cost(cost,'test',errors,[],input_schema={'body':{'limit':{'type':'integer'}}})
    assert not errors
    cost['request_count']['maximum']=20
    validator.check_cost(cost,'test',errors,[],input_schema={'body':{'limit':{'type':'integer'}}})
    assert any('request_count' in e for e in errors)
