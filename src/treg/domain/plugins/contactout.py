"""ContactOut's plugins (catalog schema v2, section 5)."""

from __future__ import annotations

from . import Answer, Request


def profiles_matched(request: Request, answer: Answer) -> int | None:
    """The Hashed Email API bills one email credit per profile that returned a hash, which the
    answer states as `matches_found`; `matches.emails` holds one entry per hash, not per profile.
    None when the answer carries no whole, non-negative count."""
    doc = answer.body
    found = doc.get("matches_found") if isinstance(doc, dict) else None
    if isinstance(found, bool) or not isinstance(found, int) or found < 0:
        return None
    return found
