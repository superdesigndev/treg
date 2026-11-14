"""AnyAPI's outcome plugins."""

from __future__ import annotations

import json

import pytest

from treg.domain.plugins import Answer, anyapi


def _answer(body: object) -> Answer:
    return Answer(status=200, headers={}, raw=json.dumps(body).encode(), body=body)


def _scrape(**data: object) -> dict:
    return {"output": {"found": True, "data": data}, "costUsd": 0.0009}


@pytest.mark.parametrize("body,missed", [
    (_scrape(statusCode=200, markdown="# Example Domain"), False),
    (_scrape(statusCode=404, markdown="# Not Found"), True),
    (_scrape(statusCode="503", markdown="# Unavailable"), True),
    (_scrape(statusCode=399, markdown="# Example"), False),
    (_scrape(statusCode=600, markdown="# Example"), False),
    (_scrape(statusCode="x", markdown="# Example"), False),
    (_scrape(statusCode=float("inf"), markdown=""), False),
    (_scrape(statusCode=float("-inf")), False),
    (_scrape(statusCode=float("inf"), markdown="# Example Domain"), False),
    (_scrape(statusCode=float("nan"), markdown=""), True),
    (_scrape(statusCode=True, markdown="# Example"), False),
    (_scrape(statusCode=200, markdown=""), True),
    (_scrape(statusCode=200, markdown=None), True),
    (_scrape(statusCode=200, markdown=[]), True),
    (_scrape(statusCode=200), True),
    (_scrape(statusCode=200, markdown={}), False),
    (_scrape(statusCode=200, markdown=0), False),
    ({"output": {"found": False, "data": None}, "costUsd": 0}, True),
    ({}, True),
    ([], True),
    (None, True),
])
def test_scrape_miss_reads_the_target_status_and_the_markdown(body, missed):
    assert anyapi.scrape_miss(None, _answer(body)) is missed


@pytest.mark.parametrize("body,errored", [
    (_scrape(statusCode=200, markdown="# Example Domain") | {"error": {"code": "upstream"}}, True),
    (_scrape(statusCode=200, markdown="# Example Domain") | {"errors": ["partial"]}, True),
    (_scrape(statusCode=200, markdown="# Example Domain") | {"error": ""}, True),
    (_scrape(statusCode=200, markdown="# Example Domain") | {"error": None, "errors": None}, False),
    (_scrape(statusCode=200, markdown="# Example Domain"), False),
    (_scrape(statusCode=404, markdown="# Not Found") | {"error": {"code": "upstream"}}, False),
    (_scrape(statusCode=200) | {"errors": ["no page"]}, False),
    (_scrape(statusCode=float("inf"), markdown="") | {"error": {"code": "upstream"}}, True),
    ({"error": {"code": "upstream"}}, False),
    ([{"error": "x"}], False),
    (None, False),
])
def test_scrape_error_is_an_envelope_beside_a_page(body, errored):
    assert anyapi.scrape_error(None, _answer(body)) is errored
