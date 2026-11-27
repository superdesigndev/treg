"""Apify: the settle quantities its rows name that a count cannot say."""

from __future__ import annotations

from . import Answer, Request


def job_starts(request: Request | None, answer: Answer | None) -> dict | None:
    """LinkedIn job search: one unit per dataset row, and one actor start per job title and
    location pair the run searches. An absent or empty `jobTitles` or `locations` list counts as
    one. None when the answer is not the bare dataset list."""
    rows = answer.body if answer is not None else None
    if not isinstance(rows, list):
        return None
    body = request.body if request is not None and isinstance(request.body, dict) else {}
    starts = 1
    for name in ("jobTitles", "locations"):
        items = body.get(name)
        starts *= max(1, len(items)) if isinstance(items, list) else 1
    return {"units": len(rows), "base": starts}


# Apify runs bill per event, and only the run option maxTotalChargeUsd bounds those events: the
# maxItems option does not bind actors whose own input sets the row count. A dataset-view option
# (limit, offset, format, unwind) would make the returned rows disagree with the events billed.
_RUN_OPTIONS = ("maxTotalChargeUsd", "maxItems", "memory", "timeout")
# A run must end, and its rows arrive, before anyone stops waiting: past Apify's 300-second
# synchronous wait it answers 408, past treg's upstream read timeout or the MCP client's 120 s the
# call fails, and each releases the hold unbilled while the run keeps billing. 90 s leaves room for
# the container start and the dataset read under the shortest of those waits.
MAX_TIMEOUT = 90


def _whole(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def run_options(request: Request) -> dict | None:
    """Treg's key takes only the run options above, each once; `timeout` from 1 to 90 seconds and
    `maxItems`, when sent, a positive integer. The spend cap itself is `reserve.money`'s check."""
    query = request.query
    problem = None
    if any(name not in _RUN_OPTIONS for name in query):
        problem = "only maxTotalChargeUsd, maxItems, memory and timeout"
    elif any(isinstance(value, list) for value in query.values()):
        problem = "each run option at most once"
    elif not 1 <= (_whole(query.get("timeout")) or 0) <= MAX_TIMEOUT:
        problem = f"timeout from 1 to {MAX_TIMEOUT} seconds"
    elif "maxItems" in query and (_whole(query["maxItems"]) or 0) < 1:
        problem = "maxItems as a positive integer"
    if problem is None:
        return None
    return {"parameter": "queryParams", "expected": problem,
            "message": f"Apify platform calls take {problem}; maxTotalChargeUsd is the spend cap Apify "
                       "enforces and the hold. Connect your own key for larger runs"}
