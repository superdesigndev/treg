"""AnyAPI: the outcome rules its rows name that the predicate cannot say."""

from __future__ import annotations

import math

from . import Answer, Request


def scrape_miss(request: Request | None, answer: Answer) -> bool:
    """A scrape that brought back no page: the target site answered an HTTP error (a 4xx or 5xx in
    `output.data.statusCode`, a number or a numeric string, is the site's error page, not the page),
    or `output.data.markdown` is missing, null, empty or an empty list. An infinite `statusCode` is
    never a miss, whatever the markdown says."""
    data = answer.body
    for key in ("output", "data"):
        data = data.get(key) if isinstance(data, dict) else None
    data = data if isinstance(data, dict) else {}
    status = data.get("statusCode")
    if isinstance(status, float) and math.isinf(status):
        return False
    try:
        failed = 400 <= int(status) <= 599
    except (TypeError, ValueError, OverflowError):
        failed = False
    return failed or data.get("markdown") in (None, "", [])


def scrape_error(request: Request | None, answer: Answer) -> bool:
    """A 2xx error envelope (a non-null `error` or `errors`) beside a page. One beside a scrape miss
    is the miss, so it stays free."""
    body = answer.body
    enveloped = isinstance(body, dict) and (body.get("error") is not None or body.get("errors") is not None)
    return enveloped and not scrape_miss(request, answer)
