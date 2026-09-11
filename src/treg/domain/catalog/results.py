"""Conservative result evidence for cache admission. Never rewrites provider bytes."""
from dataclasses import dataclass
import json
import math
import re
from typing import Literal


STRICT_ENDPOINTS = frozenset({
    "leadsforge.people.email.find",
    "hunter.companies.emails",
    "leadmagic.x.employee-finder",
    "seranking.google.keywords.volume",
})


@dataclass(frozen=True)
class Result:
    state: Literal["found", "empty", "error", "unknown"]
    reason: str

    @property
    def hit(self) -> bool | None:
        return {"found": True, "empty": False}.get(self.state)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def has_result_rules(endpoint_id: str) -> bool:
    """Only verified, configured hit/miss adapters opt into result-aware cache behavior."""
    from .store import load

    adapter = load().adapters.get(endpoint_id)
    return adapter is not None and adapter.verified and bool(adapter.miss.strip())


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
        return Result("error", "provider_error")
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
        if (status == "succeeded" and isinstance(email, str)
                and re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", email)):
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
