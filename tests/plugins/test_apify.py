"""Apify's settle plugins."""

from __future__ import annotations

import json

import pytest

from treg.domain.plugins import Answer, Request, apify


def _answer(body: object) -> Answer:
    return Answer(status=201, headers={}, raw=json.dumps(body).encode(), body=body)


@pytest.mark.parametrize("body,rows,quantity", [
    ({"jobTitles": ["a", "b"], "locations": ["x", "y", "z"]}, [{}, {}], {"units": 2, "base": 6}),
    ({"jobTitles": ["a"]}, [], {"units": 0, "base": 1}),
    ({"jobTitles": [], "locations": "x"}, [{}], {"units": 1, "base": 1}),
    (None, [{}], {"units": 1, "base": 1}),
    ({"jobTitles": ["a", "b"]}, {"error": "x"}, None),
])
def test_job_starts_counts_rows_and_one_start_per_title_and_location(body, rows, quantity):
    request = Request(method="POST", query={}, body=body)
    assert apify.job_starts(request, _answer(rows)) == quantity
