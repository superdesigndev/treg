"""Hunter's plugins (catalog schema v2, section 5)."""

from __future__ import annotations

import re

from . import Answer, Request

_EMAIL = re.compile(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+")


def _data(answer: Answer) -> object:
    """`data` of a JSON-object answer that carries no error envelope, else None."""
    doc = answer.body
    if not isinstance(doc, dict) or doc.get("error") or doc.get("errors"):
        return None
    return doc.get("data")


def emails_returned(request: Request, answer: Answer) -> int | None:
    """Domain Search's billed emails: those it returned, rounded up to a whole ten, because Hunter
    takes one search credit for every ten it starts. None when the answer carries no email list."""
    data = answer.body.get("data") if isinstance(answer.body, dict) else None
    emails = data.get("emails") if isinstance(data, dict) else None
    if not isinstance(emails, list):
        return None
    return -(-len(emails) // 10) * 10


def finder_unanswered(request: Request, answer: Answer) -> bool:
    """A JSON answer from Email Finder that is neither an address in `data.email` nor a null there:
    an error envelope, no `data.email` field, or a value that is not an email address."""
    if answer.body is None:
        return False
    data = _data(answer)
    if not isinstance(data, dict) or "email" not in data:
        return True
    email = data["email"]
    return email is not None and not (isinstance(email, str) and _EMAIL.fullmatch(email))


def domain_search_unanswered(request: Request, answer: Answer) -> bool:
    """A JSON answer from Domain Search that is neither a list of rows with an address in `value`
    nor an empty list: an error envelope, no `data.emails` list, or a row without an address."""
    if answer.body is None:
        return False
    data = _data(answer)
    rows = data.get("emails") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return True
    return not all(isinstance(row, dict) and isinstance(row.get("value"), str) and row["value"].strip()
                   for row in rows)
