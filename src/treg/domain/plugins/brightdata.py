"""Bright Data: records delivered by a Web Scraper answer, the provider's billed unit."""

from __future__ import annotations

import json

from . import Answer, Request


def records(request: Request, answer: Answer) -> int | None:
    """How many records the answer delivered, or None when it cannot be counted.

    Bright Data reports no charge, so the body is the only evidence. Its delivery formats: a JSON
    array (one element per record), NDJSON or JSON Lines (one object per line) and CSV (a header
    line, then one line per record). A JSON object is never records: the over-one-minute fallback
    and a trigger answer `{snapshot_id}`, and an early snapshot download answers its running state.
    A gzip download (`compress=true`), a body that starts like JSON but does not parse, and any
    other shape cannot be counted. With `include_errors`, an input that found nothing (`dead_page`,
    for one) comes back as a record with an `error_code`; Bright Data counts it as an error, not a
    record, and does not bill it."""
    raw = answer.raw
    if raw[:2] == b"\x1f\x8b":
        return None
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return 0
    try:
        doc = json.loads(text)
    except ValueError:
        lines = [line for line in text.splitlines() if line.strip()]
        try:
            return _billed([json.loads(line) for line in lines])
        except ValueError:
            pass
        if text[0] in "[{":
            return None
        return len(lines) - 1 if len(lines) > 1 else None
    if isinstance(doc, list):
        return _billed(doc)
    if isinstance(doc, dict):
        return 0
    return None


def _billed(items: list) -> int:
    return sum(1 for item in items if not (isinstance(item, dict) and "error_code" in item))
