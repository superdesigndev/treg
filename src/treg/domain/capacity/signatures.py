"""The signature table (plan §4.1): what an upstream error answer means for OUR account, or that no
account was consulted at all.

One table shared by the sweep, the future call-path trigger and the alerts, so "exhausted" means
one thing everywhere. Pure functions over (provider, status, headers, body).

  balance  — the account is out of money/credits → exhausted until a top-up (no reset time)
  quota    — the period allowance is used up (a 429 wearing quota clothes) → exhausted until reset
  burst    — a genuine rate limit with a short retry-after → smoothed, NEVER exhausted
  unknown  - a 429 we cannot classify and whose body names no capacity phrase → logged for
             classification (treated as burst: never refuse)
  edge_block - the vendor's CDN refused the request's shape before the vendor's code saw it → NEVER
             exhausted, only recorded (so a chart can attribute it to a UA family)
  unrecorded - a 4xx no row matched whose body still names credits/quota/balance → NEVER exhausted,
             only logged/counted: the tripwire for a vendor whose out-of-credit answer is not in
             the table yet (how Apollo's 422 went unseen for eleven hours, 2026-09-01)
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
    # Apollo: an empty credit pool is a 422 {"error": "Insufficient credits. Please upgrade your
    # plan."} (ops/capacity.md, 2026-09-01). A 422 without the phrase is the caller's validation error.
    ("apollo", 422, r"insufficient credits", "balance"),
    # Moz: a spent row quota is a 403 {"error": "The account does not have enough quota remaining for
    # current period.", "data": {"issue": "insufficient-quota"}} — 115 of one org's calls hit it
    # unrecognised on 2026-09-04 (nothing here matched a 403, and "quota" alone is not a tripwire
    # word). The period resets on Moz's billing day, which the answer does not name.
    ("moz", 403, r"insufficient-quota", "quota"),
    ("*", 402, r"", "balance"),
]

# The `unrecorded` tripwire's vocabulary, hand-kept: every capacity PHRASE the table records for
# some vendor (recording one vendor's wording arms the tripwire for every other) plus the generic
# nouns. Period words from the 429 rows ("daily", "monthly", "per day") are NOT capacity words and
# stay out; a bare "insufficient", "quota" or "balance" would flag "insufficient parameters",
# "quotation mark" and "unbalanced quotes" - a caller's error echoed back. The test
# `test_every_recorded_phrase_arms_the_tripwire` keeps this list and the table in step.
CAPACITY_PHRASES = (
    r"not enough credits", r"insufficient[ _]credits", r"nocreditsremaining", r"payment required",
    r"reached your credit limit", r"exceeded the monthly request limit",
    r"insufficient (?:credits?|balance|funds)", r"out of credits?", r"credits? (?:exhausted|remaining|left)",
    r"(?:account |api |credit )?(?:balance|quota)(?: (?:has been|is|was))? (?:exceeded|reached|exhausted|limit)",
    r"upgrade your plan", r"insufficient-quota", r"not have enough quota",
)
_UNRECORDED = re.compile(r"\b(?:" + "|".join(f"(?:{p})" for p in CAPACITY_PHRASES) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class Signal:
    kind: str                    # balance | quota | burst | unknown | edge_block | unrecorded
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


_CF_1XXX = re.compile(r"cloudflare-1xxx-errors|error 10\d\d: access denied")


def _edge_block(status: int, headers, text_l: str) -> bool:
    """Never the User-Agent: Cloudflare's `cf-mitigated` marker, its HTML block page where the
    vendor's app would have answered JSON, or its 1xxx problem-JSON (error 1010 is the browser
    signature block)."""
    if status in (403, 503) and _header(headers, "cf-mitigated") is not None:
        return True
    if status != 403:
        return False
    if ("cloudflare" in (_header(headers, "server") or "").lower()
            and "text/html" in (_header(headers, "content-type") or "").lower()):
        return True
    return _CF_1XXX.search(text_l) is not None


def classify(provider: str, status: int, headers=None, body: bytes | str = b"",
             now: datetime | None = None) -> Signal | None:
    """The capacity meaning of one upstream answer, or None when it has none."""
    now = now or utcnow_naive()
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    text_l = text.lower()
    provider = (provider or "").lower()
    if _edge_block(status, headers, text_l):  # before the table: a block page carries no vendor body to match
        return Signal("edge_block", None, None, detail=text[:120])
    for prov, st, pattern, kind in _TABLE:
        if st != status or prov not in ("*", provider):
            continue
        if pattern and not re.search(pattern, text, re.IGNORECASE):
            continue
        resets = _quota_reset(provider, kind, headers, now)
        return Signal(kind, resets, None, detail=text[:120])
    if status == 429:
        wait = _retry_after(headers, now)
        if wait is not None and wait <= BURST_MAX_RETRY_AFTER_S:
            return Signal("burst", None, wait, detail=text[:120])
        if wait is not None:  # the provider named a long wait: a period allowance, not a burst
            return Signal("quota", now + timedelta(seconds=wait), None, detail=text[:120])
    if 400 <= status < 500 and status not in (401, 404):  # 401 is the key, 404 the resource
        m = _UNRECORDED.search(text)
        if m:  # detail is the PHRASE, never the body: a vendor's error often echoes the request back
            return Signal("unrecorded", None, None, detail=m.group(0).lower())
    if status == 429:
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
    burst, unknown, edge_block and unrecorded never refuse a call (plan §4.1)."""
    return signal is not None and signal.kind in ("balance", "quota")
