"""The signature table (plan §4.1): what a provider's error body means for OUR account.

One table shared by the sweep, the future call-path trigger and the alerts, so "exhausted" means
one thing everywhere. Pure functions over (provider, status, headers, body).

  balance  — the account is out of money/credits → exhausted until a top-up (no reset time)
  quota    — the period allowance is used up (a 429 wearing quota clothes) → exhausted until reset
  burst    — a genuine rate limit with a short retry-after → smoothed, NEVER exhausted
  unknown  — a 429 we cannot classify → logged for classification (treated as burst: never refuse)
  None     — not a capacity signal at all (caller-caused 4xx, 5xx, success)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...timeutil import utcnow_naive

BURST_MAX_RETRY_AFTER_S = 60

# provider → (status, substring-regex on the body, kind). `*` = any provider. First match wins.
_TABLE: list[tuple[str, int, str, str]] = [
    ("findymail", 402, r"not enough credits", "balance"),
    ("leadsforge", 402, r"insufficient_credits", "balance"),
    ("leadmagic", 402, r"insufficient_credits", "balance"),
    ("thecompaniesapi", 403, r"noCreditsRemaining", "balance"),
    ("companyenrich", 402, r"payment required", "balance"),
    ("akta", 402, r"insufficient credits", "balance"),
    ("lusha", 400, r"reached your credit limit", "balance"),
    ("predictleads", 402, r"exceeded the monthly request limit", "quota"),
    ("lusha", 429, r"daily", "quota"),
    ("hunter", 429, r"per billing period", "quota"),
    ("apollo", 429, r"per (day|month)|daily|monthly", "quota"),
    ("*", 402, r"", "balance"),
]


@dataclass(frozen=True)
class Signal:
    kind: str                    # balance | quota | burst | unknown
    resets_at: datetime | None   # when the account serves again, if the provider told us
    retry_after_s: int | None    # for burst: how long the provider asked us to wait
    detail: str = ""


def _header(headers, name: str) -> str | None:
    if headers is None:
        return None
    if hasattr(headers, "get"):
        v = headers.get(name)
        return None if v is None else str(v)
    wanted = name.lower().encode("latin-1")
    for k, v in headers:
        if k.lower() == wanted:
            return v.decode("latin-1")
    return None


def _retry_after(headers, now: datetime) -> int | None:
    ra = _header(headers, "retry-after")
    if ra is not None:
        try:
            return max(0, int(float(ra)))
        except ValueError:
            pass
    reset = _header(headers, "x-ratelimit-reset")
    if reset is not None:
        try:
            n = float(reset)
        except ValueError:
            return None
        # epoch seconds vs seconds-from-now: anything over a year is an epoch
        if n > 365 * 86400:
            return max(0, int(n - now.timestamp()))
        return max(0, int(n))
    return None


def classify(provider: str, status: int, headers=None, body: bytes | str = b"",
             now: datetime | None = None) -> Signal | None:
    """The capacity meaning of one upstream answer, or None when it has none."""
    now = now or utcnow_naive()
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    text_l = text.lower()
    provider = (provider or "").lower()
    for prov, st, pattern, kind in _TABLE:
        if st != status or prov not in ("*", provider):
            continue
        if pattern and not re.search(pattern, text_l if pattern.islower() else text):
            continue
        resets = _quota_reset(provider, kind, headers, now)
        return Signal(kind, resets, None, detail=text[:120])
    if status == 429:
        wait = _retry_after(headers, now)
        if wait is not None and wait <= BURST_MAX_RETRY_AFTER_S:
            return Signal("burst", None, wait, detail=text[:120])
        if wait is not None:  # the provider named a long wait: a period allowance, not a burst
            return Signal("quota", now + timedelta(seconds=wait), None, detail=text[:120])
        return Signal("unknown", None, None, detail=text[:120])
    return None


def _quota_reset(provider: str, kind: str, headers, now: datetime) -> datetime | None:
    if kind != "quota":
        return None
    wait = _retry_after(headers, now)
    if wait is not None:
        return now + timedelta(seconds=wait)
    if provider == "lusha":  # "Daily … limit": resets at the next UTC midnight (plan §4.1)
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return None  # billing-period quotas: the sweep's account call carries the real reset date


def is_exhausting(signal: Signal | None) -> bool:
    """Does this signal mark the account exhausted? Only confirmed balance/quota signatures do;
    burst and unknown never refuse a call (plan §4.1)."""
    return signal is not None and signal.kind in ("balance", "quota")
