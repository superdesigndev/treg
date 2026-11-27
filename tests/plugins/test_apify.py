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


@pytest.mark.parametrize("query,expected", [
    ({"maxTotalChargeUsd": "0.04", "timeout": "60"}, None),
    ({"maxTotalChargeUsd": "0.5", "maxItems": "3", "memory": "1024", "timeout": "90"}, None),
    ({"maxTotalChargeUsd": "0.04", "timeout": "91"}, "timeout from 1 to 90 seconds"),
    ({"maxTotalChargeUsd": "0.04", "timeout": "0"}, "timeout from 1 to 90 seconds"),
    ({"maxTotalChargeUsd": "0.04"}, "timeout from 1 to 90 seconds"),
    ({"maxTotalChargeUsd": "0.04", "limit": "1", "timeout": "60"}, "only maxTotalChargeUsd, maxItems, memory and timeout"),
    ({"maxTotalChargeUsd": "0.5", "format": "csv"}, "only maxTotalChargeUsd, maxItems, memory and timeout"),
    ({"maxTotalChargeUsd": ["0.5", "0.1"], "timeout": "60"}, "each run option at most once"),
    ({"maxTotalChargeUsd": "0.5", "maxItems": ["2", "1"], "timeout": "60"}, "each run option at most once"),
    ({"maxTotalChargeUsd": "0.5", "maxItems": "²", "timeout": "60"}, "maxItems as a positive integer"),
    ({"maxTotalChargeUsd": "0.5", "maxItems": "0", "timeout": "60"}, "maxItems as a positive integer"),
])
def test_run_options_serve_only_the_bounded_run_options(query, expected):
    reason = apify.run_options(Request(method="POST", query=query, body={}))
    if expected is None:
        assert reason is None
    else:
        assert reason["parameter"] == "queryParams" and reason["expected"] == expected


def test_run_options_timeout_fits_inside_treg_s_upstream_wait():
    from treg.config import Settings
    assert apify.MAX_TIMEOUT + 30 <= Settings().call_timeout_s
