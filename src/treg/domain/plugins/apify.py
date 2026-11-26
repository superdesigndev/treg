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
