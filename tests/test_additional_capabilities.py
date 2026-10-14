"""Additional routing categories must pass the same fixture gate as the primary category."""
from pathlib import Path

import pytest

from treg.domain.catalog.routing.contracts import load_routing


@pytest.mark.parametrize('extra,expected', [
    ('valid', ('valid',)), ('unknown', ()), ('missing_output', ()), ('different_filters', ()),
])
def test_secondary_contract_is_verified_independently(extra, expected):
    primary = {'identity': [{'name': 'str'}], 'output': {'name': {'type': 'str', 'required': True}}, 'filters': {}}
    contracts = {'primary': primary, 'valid': primary,
                 'missing_output': {**primary, 'output': {'email': {'type': 'str', 'required': True}}},
                 'different_filters': {**primary, 'filters': {'country': {'type': 'str'}}}}
    adapter = {'accepts': [['name']], 'in': {'name': 'queryParams.name'},
               'out': {'name': 'name'}, 'miss': 'name == null', 'additional_capabilities': [extra]}
    docs = {'contracts.yaml': {'contracts': contracts}, 'adapters.yaml': {'adapters': {'example.lookup': adapter}}}
    endpoint = {'capability': 'primary', 'test_request': {'queryParams': {'name': 'Example'}}}
    _, adapters = load_routing(Path('.'), {'example.lookup': endpoint},
                               lambda p: docs[p.name], lambda ep: {'name': 'Example'})
    assert adapters['example.lookup'].verified
    assert adapters['example.lookup'].verified_capabilities == expected
