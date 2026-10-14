"""Conservative result evidence for cache admission. Never rewrites provider bytes."""
from dataclasses import dataclass
import json
import logging
import math
import re
from typing import Literal


STRICT_ENDPOINTS = frozenset({
    "leadsforge.people.email.find",
    "hunter.people.email.find",
    "findymail.search.name",
    "hunter.companies.emails",
    "leadmagic.x.employee-finder",
    "seranking.google.keywords.volume",
})


@dataclass(frozen=True)
class Result:
    state: Literal["found", "empty", "error", "unknown"]
    reason: str
    # A 2xx that names its own error AND that the adapter judges empty: nothing was found. Kept
    # beside `state` so cache admission still reads `error`, while the hit verdict reads a miss.
    empty: bool = False

    @property
    def hit(self) -> bool | None:
        # A 200 whose body names its own error (`errors: [{"error": "page_not_found"}]`) and carries
        # nothing found is a miss for the hit verdict; one that names an error beside real results
        # (one failed page of several) stays undecided. The state stays `error` for cache admission.
        if self.state == "error" and self.empty:
            return False
        return {"found": True, "empty": False}.get(self.state)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _email(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", value))


def has_result_rules(endpoint_id: str) -> bool:
    """Only verified, configured hit/miss adapters opt into result-aware cache behavior."""
    from .store import load

    adapter = load().adapters.get(endpoint_id)
    return adapter is not None and adapter.verified and bool(adapter.miss.strip())


def verdict(endpoint_id: str, status: int, body: bytes) -> str | None:
    """The contract's verdict word for one answer (`valid`, `catch_all`, `verified`...), or None.

    None when the endpoint's verified adapter records no verdict, the call failed, the body is not
    JSON, or the answer does not fill the contract's required output. A provider word the adapter
    does not map is also None, logged so the gap is seen rather than counted as something else.
    """
    if not 200 <= status < 300:
        return None
    from .store import load

    # Runs inside the call's audit step: whatever goes wrong here costs the word, never the call.
    try:
        adapter = load().adapters.get(endpoint_id)
        if adapter is None or not adapter.verified or not adapter.records_verdict:
            return None
        doc = json.loads(body)
        raw = adapter.verdict_raw(doc) if isinstance(doc, (dict, list)) else None
        word = adapter.verdict_word(raw)
    except Exception:  # noqa: BLE001 - an unreadable body or a failing expression is no verdict
        return None
    if raw is not None and word is None:
        logging.getLogger("treg.catalog").warning(
            "unmapped verdict word %r from %s; add it to the adapter's `verdicts`", raw[:40], endpoint_id)
    return word


def classify(endpoint_id: str, status: int, body: bytes) -> Result:
    if not 200 <= status < 300:
        return Result("error", "http_error")
    if not has_result_rules(endpoint_id):
        return Result("unknown", "unsupported_endpoint")
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError):
        return Result("unknown", "invalid_json")
    if isinstance(doc, dict) and (doc.get("error") or doc.get("errors")):
        empty = False
        if endpoint_id not in STRICT_ENDPOINTS:
            from .store import load
            try:
                empty = bool(load().adapters[endpoint_id].is_miss(doc))
            except Exception:  # noqa: BLE001 - a predicate failure is no verdict
                empty = False
        return Result("error", "provider_error", empty=empty)
    if endpoint_id not in STRICT_ENDPOINTS:
        from .store import load

        if not isinstance(doc, (dict, list)):
            return Result("unknown", "invalid_shape")
        try:
            miss = load().adapters[endpoint_id].is_miss(doc)
        except Exception:  # a predicate failure is not evidence of a business change
            return Result("unknown", "predicate_error")
        return Result("empty", "adapter_miss") if miss else Result("found", "adapter_hit")
    if endpoint_id == "leadsforge.people.email.find":
        if not isinstance(doc, dict):
            return Result("unknown", "invalid_shape")
        status, email = doc.get("status"), doc.get("email")
        if status == "not_found" and email is None:
            return Result("empty", "no_email")
        if status == "succeeded" and _email(email):
            return Result("found", "email")
        return Result("unknown", "invalid_shape")
    if endpoint_id in ("hunter.people.email.find", "findymail.search.name"):
        field = "data" if endpoint_id == "hunter.people.email.find" else "contact"
        if not isinstance(doc, dict) or field not in doc:
            return Result("unknown", "invalid_shape")
        person = doc[field]
        if field == "contact" and person is None:
            return Result("empty", "no_email")
        if not isinstance(person, dict) or "email" not in person:
            return Result("unknown", "invalid_shape")
        if person["email"] is None:
            return Result("empty", "no_email")
        if _email(person["email"]):
            return Result("found", "email")
        return Result("unknown", "invalid_shape")
    if endpoint_id == "seranking.google.keywords.volume":
        if not isinstance(doc, list):
            return Result("unknown", "invalid_shape")
        found = False
        for row in doc:
            if not isinstance(row, dict) or type(row.get("is_data_found")) is not bool:
                return Result("unknown", "invalid_shape")
            if row["is_data_found"]:
                volume = row.get("volume")
                if (not _text(row.get("keyword")) or type(volume) not in (int, float)
                        or (isinstance(volume, float) and not math.isfinite(volume)) or volume < 0):
                    return Result("unknown", "invalid_shape")
                found = True
        return Result("found", "keyword_data") if found else Result("empty", "no_keyword_data")
    if not isinstance(doc, dict):
        return Result("unknown", "invalid_shape")
    if endpoint_id == "hunter.companies.emails":
        data = doc.get("data")
        rows = data.get("emails") if isinstance(data, dict) else None
        fields, reason = ("value",), "emails"
    else:
        rows = doc.get("data")
        fields, reason = ("name", "full_name", "first_name", "profile_url"), "people"
    if not isinstance(rows, list):
        return Result("unknown", "invalid_shape")
    if not rows:
        return Result("empty", "no_" + reason)
    if all(isinstance(row, dict) and any(_text(row.get(f)) for f in fields) for row in rows):
        return Result("found", reason)
    return Result("unknown", "invalid_shape")
