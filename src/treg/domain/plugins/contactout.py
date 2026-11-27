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


# ContactOut bills one verifier credit per email whose verdict is one of these; unknown and
# disposable verdicts are free.
_BILLED_VERDICTS = frozenset({"valid", "invalid", "accept_all"})


def _results(answer: Answer) -> dict | None:
    data = answer.body.get("data") if isinstance(answer.body, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, dict) else None


def verdicts_billed(request: Request, answer: Answer) -> int | None:
    """The bulk verifier's finished job: one credit per email with a billed verdict. None when the
    answer carries no `data.result` object."""
    result = _results(answer)
    if result is None:
        return None
    return sum(1 for verdict in result.values() if verdict in _BILLED_VERDICTS)


def _contact_types(request: Request) -> tuple[str, ...]:
    """The contact types a bulk ContactInfo request asks for: `email_type` (default both email
    types, `none` for neither) and `phone` when `include_phone` is true. A value read neither way
    asks for every type, so the hold covers whatever ContactOut makes of it."""
    body = request.body if isinstance(request.body, dict) else {}
    email_type = body.get("email_type")
    asked = {a.strip().lower() for a in email_type.split(",")} if isinstance(email_type, str) else set()
    if asked != {"none"} and not asked & {"work", "personal"}:
        asked = {"work", "personal"}
    kinds = tuple(f"{kind}_email" for kind in ("work", "personal") if kind in asked)
    phone = body.get("include_phone")
    return kinds + (("phone",) if phone not in (None, False, "false", 0, "0") else ())


def contacts_held(request: Request) -> dict[str, int]:
    """Every profile sent, held at each contact type it asks for."""
    body = request.body if isinstance(request.body, dict) else {}
    profiles = body.get("profiles")
    count = len(profiles) if isinstance(profiles, list) else 0
    return {kind: count for kind in _contact_types(request)}


_FOUND = {"work_email": "work_emails", "personal_email": "personal_emails", "phone": "phones"}


def contacts_found(request: Request, answer: Answer) -> dict[str, int] | None:
    """The finished bulk ContactInfo job: per asked contact type, the profiles that returned at
    least one, as single lookups bill them. A type the request did not ask for is never counted.
    None when the answer carries no `data.result` object."""
    result = _results(answer)
    if result is None:
        return None
    profiles = [p for p in result.values() if isinstance(p, dict)]
    return {kind: sum(1 for p in profiles if isinstance(p.get(_FOUND[kind]), list) and p[_FOUND[kind]])
            for kind in _contact_types(request)}
