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
