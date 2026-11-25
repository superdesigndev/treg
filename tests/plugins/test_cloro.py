import json

import pytest

from treg.application.call.resolve import QueryValues, price_call
from treg.domain.catalog import store


@pytest.mark.parametrize(("options", "credits"), [
    ({}, 5),
    ({"include": {"html": True, "aioverview": False}}, 5),
    ({"include": {"aioverview": True}}, 7),
    ({"include": {"aioverview": {}}}, 7),
    ({"include": {"aioverview": {"markdown": False}}}, 7),
    ({"first": 41, "include": {"aioverview": {"markdown": True}}}, 7),
    ({"pages": 3}, 9),
    ({"pages": "3", "include": {"aioverview": True}}, 11),
    ({"pages": 10}, 23),
    ({"pages": 10, "include": {"aioverview": {"markdown": True}}}, 25),
    ({"pages": "invalid"}, 23),
])
def test_bing_search_units_hold_the_requested_pages_and_overview(options, credits):
    catalog = store.load()
    endpoint = catalog.by_id["cloro.bing.serp.organic"]
    body = json.dumps({"country": "US", "query": "treg", **options}).encode()
    price = price_call(endpoint, QueryValues(()), body, set())
    assert price.reported_charge_unit_micro > 0
    assert price.estimate_micro == credits * price.reported_charge_unit_micro
