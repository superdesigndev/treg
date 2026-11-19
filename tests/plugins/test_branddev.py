"""Context.dev's outcome plugins."""

from __future__ import annotations

import json

import pytest

from treg.domain.plugins import Answer, branddev

_WALL = "Just a moment... Enable JavaScript and cookies to continue. Performance and security by Cloudflare"
_PAGE = "# Example Domain\n\nThis domain is for use in illustrative examples in documents. " * 3


def _answer(body: object) -> Answer:
    return Answer(status=200, headers={}, raw=json.dumps(body).encode(), body=body)


def _scrape(**formats: object) -> dict:
    empty = {"requested": False, "success": None, "data": None}
    return {"url": "https://example.com", **{name: empty for name in ("markdown", "html", "images", "screenshot")},
            **{name: {"requested": True, "success": data is not None, "data": data}
               for name, data in formats.items()},
            "key_metadata": {"credits_consumed": 1}}


@pytest.mark.parametrize("body,missed", [
    (_scrape(markdown=_PAGE), False),
    (_scrape(html="<html><body>Example</body></html>"), False),
    (_scrape(images=[{"src": "https://example.com/a.png"}]), False),
    (_scrape(screenshot="https://cdn.example.com/shot.png"), False),
    (_scrape(markdown=_WALL), True),
    (_scrape(html=f"<html><body>{_WALL}</body></html>"), False),
    (_scrape(markdown=""), True),
    (_scrape(markdown=None), True),
    (_scrape(images=[]), True),
    (_scrape(), True),
    ({"markdown": {"requested": True, "success": False, "data": None, "error_code": "NOT_FOUND"}}, True),
    ({}, True),
    ([], True),
    (None, True),
])
def test_scrape_miss_reads_every_format_and_a_wall_in_the_markdown(body, missed):
    assert branddev.scrape_miss(None, _answer(body)) is missed
