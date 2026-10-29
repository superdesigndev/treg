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
             the table yet
  None     — not a capacity signal at all (caller-caused 4xx, 5xx, success)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ...timeutil import utcnow_naive

BURST_MAX_RETRY_AFTER_S = 60

_RESETS = {
    "next_utc_midnight": lambda now: (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
}


@dataclass(frozen=True)
class Signature:
    """`provider` "*" matches any provider; `body_regex` is searched case-insensitively. First
    match wins. `resets` names when a quota with no wait header serves again; None leaves it to
    the sweep's account call. `tripwire` marks a plain capacity phrase that arms the `unrecorded`
    tripwire for every other vendor."""
    provider: str
    status: int
    body_regex: str
    kind: str
    resets: str | None = None
    tripwire: bool = False

    def __post_init__(self):
        if self.resets is not None and self.resets not in _RESETS:
            raise ValueError(f"unknown resets {self.resets!r}")


_TABLE: list[Signature] = [
    Signature("orbit", 402, r"developer_api_credits_insufficient", "balance", tripwire=True),
    Signature("enrow", 402, r"insufficient credits", "balance", tripwire=True),
    Signature("anymailfinder", 402, r"not have enough credits", "balance", tripwire=True),
    Signature("findymail", 402, r"not enough credits", "balance", tripwire=True),
    Signature("leadsforge", 402, r"insufficient_credits", "balance"),
    # MobiusEngine Jobs Data: an empty prepaid balance is a 402 {"error": {"code": "insufficient_balance", ...}}.
    Signature("mobiusengine-jobs", 402, r"insufficient_balance", "balance", tripwire=True),
    Signature("leadmagic", 402, r"insufficient_credits", "balance"),
    Signature("thecompaniesapi", 403, r"noCreditsRemaining", "balance", tripwire=True),
    Signature("companyenrich", 402, r"payment required", "balance", tripwire=True),
    Signature("akta", 402, r"insufficient credits", "balance"),
    # AI Ark: an empty credit pool is a 402 {"error": "sorry, you don't have enough credit"}.
    Signature("aiark", 402, r"don't have enough credit", "balance"),
    Signature("lusha", 400, r"reached your credit limit", "balance", tripwire=True),
    # Misses share the 400 status; only this error code means an empty account.
    Signature("prospeo", 400, r'"error_code"\s*:\s*"insufficient_credits"', "balance"),
    Signature("predictleads", 402, r"exceeded the monthly request limit", "quota", tripwire=True),
    # PDL meters operations separately: person_identify can run out while enrich still works.
    Signature("pdl", 402, r"hit your account maximum for", "quota", tripwire=True),
    Signature("lusha", 429, r"daily", "quota", resets="next_utc_midnight"),
    Signature("hunter", 429, r"per billing period", "quota"),
    # Hunter answers its per-minute limit with a 403 {"errors": [{"id": "too_many_requests"}]} and `RateLimit-Reset`.
    Signature("hunter", 403, r"too_many_requests", "burst"),
    Signature("apollo", 429, r"per (day|month)|daily|monthly", "quota"),
    # Apollo: an empty credit pool is a 422 {"error": "Insufficient credits. Please upgrade your
    # plan."}. A 422 without the phrase is the caller's validation error.
    Signature("apollo", 422, r"insufficient credits", "balance"),
    # Moz: a spent row quota is a 403 {"error": "The account does not have enough quota remaining for
    # current period.", "data": {"issue": "insufficient-quota"}} ("quota" alone is not a tripwire
    # word). The period resets on Moz's billing day, which the answer does not name.
    Signature("moz", 403, r"insufficient-quota", "quota", tripwire=True),
    # Discovery's allowance is distinct from the shared credit pool.
    # https://docs.influencers.club/guides/error-handling — ordinary burst 429s have Retry-After.
    Signature("influencersclub", 429, r"Discovery API credit limit reached", "quota", tripwire=True),
    # reAPI: an empty prepaid balance is a 402 {"error": {"code": 30001, "message": "Insufficient
    # credits. Required: 13856", ...}} (seen with a request larger than the balance).
    Signature("reapi", 402, r"insufficient credits", "balance"),
    # AnyAPI: a spent wallet or per-key spend cap is a 402. A trial key
    # driven past its cap answers {"error": "trial_cap_reached"}. The funded-wallet body is documented as
    # "insufficient wallet balance" (openapi.json, 402) and not yet observed.
    Signature("anyapi", 402, r"trial_cap_reached|insufficient|spend cap|balance", "balance"),
    Signature("trykitt", 418, r"temporarily throttled", "burst"),
    Signature("trykitt", 402, r"insufficient (?:credits?|funds|balance)|out of credits", "balance"),
    # This API uses 402 for both funds and rate limits. The first matching row wins.
    Signature("trykitt", 402, r"", "unknown"),
    # ContactOut documents this 403 separately from "No access to endpoint".
    # Independent pools: lock only the failed endpoint, never the entire provider.
    # https://api.contactout.com/#errors
    Signature("contactout", 403, r"you're out of credits", "quota", tripwire=True),
    # cloro: a spent credit allowance is a 403 ForbiddenError with `error.code: "INSUFFICIENT_CREDITS"`
    # (OpenAPI 3.1 spec; documented, not yet observed). Its 429s are CONCURRENT_LIMIT_EXCEEDED /
    # RATE_LIMIT_EXCEEDED bursts with X-RateLimit-* headers, never a period quota; the plan
    # allowance resets monthly at `cycleResetsAt` from GET /v1/credits.
    Signature("cloro", 403, r"insufficient_credits", "balance"),
    Signature("crawl4ai", 402, r"no_credit", "balance", tripwire=True),
    Signature("crawl4ai", 402, r"spend_cap|plan_cap", "quota"),
    Signature("crawl4ai", 429, r"", "burst"),
    # Tavily documents distinct custom statuses for the monthly plan allowance and the operator's
    # PAYGO ceiling. Both are quotas: the first resets with the plan period; the second serves again
    # when the operator raises its dashboard limit. Neither is a transient 429 burst.
    Signature("tavily", 432, r"exceeds your plan's set usage limit", "quota", tripwire=True),
    Signature("tavily", 433, r"exceeds the pay-as-you-go limit", "quota", tripwire=True),
    # Icypeas answers an empty credit pool with a 200: {"validationErrors": [{"message":
    # "insufficient_credits", "type": "InsufficientCredits", ...}], "success": false}, on every paid
    # route. The only 2xx row: callers of `classify` that
    # gate on >= 400 would never see it.
    Signature("icypeas", 200, r'"message"\s*:\s*"insufficient_credits"', "balance"),
    # HLR Lookup answers an empty account with a 200 whose `results[0].error` is
    # INSUFFICIENT_CREDIT (its docs; the catalog row's `expect` already keeps it unbilled).
    Signature("hlrlookup", 200, r'"error"\s*:\s*"insufficient_credit"', "balance"),
    # Tomba: a route's spent allowance is a 429 "You have reached your usage limit", with no Retry-After.
    Signature("tomba", 429, r"reached your usage limit", "quota", tripwire=True),
    # Fish Audio: too many requests in flight is a 429 "exceeded your current concurrency limit", no Retry-After.
    Signature("fishaudio", 429, r"concurrency limit", "burst"),
    # Icypeas: its per-minute limit is a 429 "exceeded the max number of requests per minute", no Retry-After.
    Signature("icypeas", 429, r"max number of requests per minute", "burst"),
    Signature("*", 402, r"", "balance"),
]

# The `unrecorded` tripwire's vocabulary: generic capacity nouns plus every row's `tripwire`
# phrase, so recording one vendor's wording arms the tripwire for every other. A row's regex
# never joins by itself: anyapi's `insufficient|balance` would flag "insufficient parameters", a
# caller's error echoed back, and the 429 rows carry period words like "daily".
_GENERIC_PHRASES = (
    r"insufficient[ _]credits?", r"insufficient (?:credits?|balance|funds)", r"out of credits?",
    r"credits? (?:exhausted|remaining|left)",
    r"(?:account |api |credit )?(?:balance|quota)(?: (?:has been|is|was))? (?:exceeded|reached|exhausted|limit)",
    r"upgrade your plan", r"\w*(?:not|n['’]t) have enough (?:credits?|quota)",
)
CAPACITY_PHRASES = _GENERIC_PHRASES + tuple(sig.body_regex for sig in _TABLE if sig.tripwire)
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


_DURATION = re.compile(r"(?:(-?\d+) days?, *)?(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)")
_MAX_WAIT_S = 366 * 86400


def _seconds(value: str) -> float | None:
    try:
        n = float(value)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _delay_or_date(value: str, now: datetime) -> float | None:
    """`Retry-After`: delta seconds or an HTTP-date (RFC 9110), or a timedelta-style
    `[D day(s), ]HH:MM:SS` duration."""
    n = _seconds(value)
    if n is not None:
        return n
    m = _DURATION.fullmatch(value.strip())
    if m:
        days, h, mi, sec = m.groups()
        return int(days or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + float(sec)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return (when - now).total_seconds()


def _reset(value: str, now: datetime) -> float | None:
    """`RateLimit-Reset` / `X-RateLimit-Reset`: anything over a year is an epoch, else seconds from now."""
    n = _seconds(value)
    if n is None:
        return None
    return n - now.replace(tzinfo=timezone.utc).timestamp() if n > 365 * 86400 else n


_WAIT_HEADERS = (("retry-after", _delay_or_date), ("ratelimit-reset", _reset), ("x-ratelimit-reset", _reset))


def _retry_after(headers, now: datetime) -> int | None:
    """Seconds until the provider serves again, rounded up, from the first wait header that parses
    to at most a year; past is 0."""
    for name, parse in _WAIT_HEADERS:
        value = _header(headers, name)
        wait = None if value is None else parse(value, now)
        if wait is not None and wait <= _MAX_WAIT_S:
            return max(0, math.ceil(wait))
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
    for sig in _TABLE:
        if sig.status != status or sig.provider not in ("*", provider):
            continue
        if sig.body_regex and not re.search(sig.body_regex, text, re.IGNORECASE):
            continue
        return Signal(sig.kind, _quota_reset(sig, headers, now),
                      _retry_after(headers, now) if sig.kind == "burst" else None, detail=text[:120])
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


def _quota_reset(sig: Signature, headers, now: datetime) -> datetime | None:
    if sig.kind != "quota":
        return None
    wait = _retry_after(headers, now)
    if wait is not None:
        return now + timedelta(seconds=wait)
    if sig.resets is not None:
        return _RESETS[sig.resets](now)
    return None


EXHAUSTING_KINDS = ("balance", "quota")


def is_exhausting(signal: Signal | None) -> bool:
    """Does this signal mark the account exhausted? Only confirmed balance/quota signatures do;
    burst, unknown, edge_block and unrecorded never refuse a call (plan §4.1)."""
    return signal is not None and signal.kind in EXHAUSTING_KINDS
