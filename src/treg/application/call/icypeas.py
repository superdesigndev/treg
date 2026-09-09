"""Icypeas' asynchronous submissions: what a 2xx acknowledgement is worth at response time.

Pure billing interpretation of the unmodified request/response. Prices live in catalog YAML;
no balance polling or database access belongs in a call's settlement.

Three routes (/email-search, /domain-search, /bulk-search) answer 2xx with an acknowledgement and
ZERO result rows: `{success, item: {_id, status}}` for a single search, `{success, status:
"in_progress", file}` for a bulk job. The work runs in the background and the credit is taken only
when a search FINDS something, which is visible later on the free poll route
(icypeas.search.results.read, status DEBITED), never on the submission. treg cannot observe the
outcome at response time, so the acknowledgement settles at 0 - the same "zero records delivered
HERE" reading Bright Data's snapshot handoff gets in settle.py - and the found-email credits are
absorbed until terminal settlement exists for this provider. Before this rule every submission
settled at the 20-row page estimate, hit or miss (149 + 61 platform calls since 2026-08-20, each
charged exactly $0.38; NOT_FOUND verified free upstream 2026-09-09).

Synchronous routes (/url-search, /scrape, /reverse-email-lookups ...) carry their rows inline
under `data` and never look like an acknowledgement, so they keep the estimate.
"""

from __future__ import annotations

# Any list under one of these keys means the body carries rows - a synchronous answer or a poll
# page - and is therefore not a bare acknowledgement.
_ROW_KEYS = ("data", "items", "results", "leads", "files")


def is_submission_ack(doc) -> bool:
    """True for the two acknowledgement shapes and nothing else: a single search's
    `item._id` or a bulk job's `file` + `status`, with no row list anywhere at the top level."""
    if not isinstance(doc, dict) or doc.get("success") is not True:
        return False
    if any(isinstance(doc.get(key), list) for key in _ROW_KEYS):
        return False
    item = doc.get("item")
    if isinstance(item, dict):
        return isinstance(item.get("_id"), str) and bool(item["_id"])
    return isinstance(doc.get("file"), str) and bool(doc["file"]) and isinstance(doc.get("status"), str)


def bulk_rows(doc) -> int | None:
    """How many rows a /bulk-search body submits (its top-level `data` array), or None when the
    body does not say - an absent, empty or non-list `data` leaves the caller's default in force."""
    rows = doc.get("data") if isinstance(doc, dict) else None
    return len(rows) if isinstance(rows, list) and rows else None
