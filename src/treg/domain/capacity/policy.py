"""Capacity policies: what each treg-owned account meters, how it is funded, and the pure rule
that turns the latest snapshot into a served/exhausted state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ...models import CapacityPolicy, CapacitySnapshot
from ...timeutil import utcnow_naive
from .collectors import NO_BALANCE_API, all_platform_providers

AGGREGATORS = ("orthogonal", "monid")
"""The overflow aggregators — prepaid accounts of ours that run dry like any vendor's."""

# What we KNOW about each account as of 2026-08-28 (docs/PROVIDER-CAPACITY-PLAN.md §0, §2.2).
# capacity_type / funding_mode / source. Anything not listed imports as unknown/unknown and is
# flagged by the sweep — a policy row must be classified by a person, never guessed by code.
_KNOWN: dict[str, tuple[str, str, str]] = {
    "adyntel": ("credits", "manual", "manual"),
    "dropleads": ("credits", "manual", "api"),
    "trykitt": ("cash", "manual", "api"),
    "harvestapi": ("cash", "auto_recharge", "api"),  # Owner will enable vendor auto top-up for production.
    "fetchinio": ("credits", "manual", "api"),
    "dataforseo": ("cash", "auto_recharge", "api"),
    "tikhub": ("cash", "auto_recharge", "api"),
    "tinyfish": ("cash", "manual", "api"),
    "lusha": ("credits", "auto_recharge", "api"),
    "scrapecreators": ("credits", "manual", "api"),
    "contactout": ("credits", "unknown", "api"),  # independent pools; overages unconfirmed
    "leadmagic": ("credits", "manual", "api"),
    "findymail": ("credits", "manual", "api"),
    "leadsforge": ("credits", "manual", "api"),
    "thecompaniesapi": ("credits", "manual", "api"),
    "tomba": ("monthly_quota", "quota_reset", "api"),
    "hunter": ("monthly_quota", "quota_reset", "api"),
    "quickenrich": ("monthly_quota", "quota_reset", "api"),
    "prospeo": ("monthly_quota", "quota_reset", "api"),
    "aiark": ("monthly_quota", "quota_reset", "api"),
    "wiza": ("credits", "manual", "api"),
    "limadata": ("credits", "auto_recharge", "manual"),
    # Manually verified in the Developer Portal: vendor auto-recharge is enabled. treg neither
    # reads nor changes that setting, so the observation source remains manual.
    "trestleiq": ("cash", "auto_recharge", "manual"),
    "tavily": ("credits", "manual", "api"),
    # The API supplies the exact credit balance; vendor auto recharge was manually enabled and
    # verified in the Serper dashboard.
    "serper": ("credits", "auto_recharge", "api"),
    "keenable": ("requests", "manual", "manual"),
    "olostep": ("credits", "manual", "api"),
    # The shared account uses subscription funding; the API supplies its exact credit balance.
    "scrapegraphai": ("credits", "subscription", "api"),
    "getleadsio": ("credits", "manual", "api"),
    "sumble": ("monthly_quota", "quota_reset", "api"),
    "moltsets": ("rolling_quota", "subscription", "api"),
    "openmart": ("credits", "subscription", "api"),
    "scrubby": ("credits", "manual", "manual"),
    "predictleads": ("monthly_quota", "quota_reset", "api"),
    "companyenrich": ("credits", "manual", "api"),
    "apollo": ("credits", "manual", "api"),
    "oceanio": ("credits", "manual", "api"),
    "icypeas": ("credits", "manual", "api"),
    "fiber_ai": ("credits", "manual", "api"),
    "branddev": ("credits", "manual", "api"),
    "influencersclub": ("credits", "manual", "api"),
    "pdl": ("credits", "manual", "headers"),
    "serpapi": ("monthly_quota", "quota_reset", "api"),
    "serpstat": ("monthly_quota", "quota_reset", "api"),
    "spyfu": ("monthly_quota", "quota_reset", "api"),
    "coingecko": ("monthly_quota", "quota_reset", "api"),
    "seranking": ("credits", "manual", "api"),
    "moz": ("monthly_quota", "quota_reset", "api"),
    "diffbot": ("monthly_quota", "quota_reset", "api"),
    "apify": ("cash", "manual", "api"),
    "twelvedata": ("requests", "subscription", "api"),
    "financialdatasets": ("credits", "auto_recharge", "manual"),
    "fishaudio": ("cash", "manual", "api"),
    "bounceban": ("credits", "manual", "api"),
    # The free API reports only the current balance, not whether vendor Auto-Pay is enabled.
    # Treat replenishment as manual until that account setting is explicitly verified.
    "zerobounce": ("credits", "manual", "api"),
    "datagma": ("credits", "manual", "api"),
    # Neither aggregator exposes a balance endpoint at its documented path (plan §7).
    "overflow:orthogonal": ("cash", "manual", "manual"),
    "overflow:monid": ("cash", "manual", "manual"),
}

# Verified quota/rate facts (plan §0.1, §4.1). A provider can carry both.
_QUOTAS: dict[str, dict] = {
    "lusha": {"limit": None, "period": "day", "resets_at_rule": "local_midnight"},
    "hunter": {"limit": None, "period": "billing", "resets_at_rule": "account.reset_date"},
    "quickenrich": {"limit": None, "period": "billing", "resets_at_rule": "subscription renewal; no reset timestamp in API"},
    "prospeo": {"limit": None, "period": "billing", "resets_at_rule": "account.next_quota_renewal_date"},
    "aiark": {"limit": 15000, "period": "billing", "resets_at_rule": "monthly subscription; date not reported by API"},
}
_RATE_LIMITS: dict[str, dict] = {
    # The account reports 5 requests/s, but /post/engagement consumes two rate-limit units. The
    # provider-wide limiter cannot weight one endpoint, so two calls/s is the safe shared-key pace.
    "fetchinio": {"limit": 2, "window_s": 1, "source": "policy"},
    "adyntel": {"limit": 5, "window_s": 1, "source": "docs"},
    # Search's documented burst allowance is the strictest request-count limit shared by these
    # hosts. Fetch additionally meters URLs and Agent limits concurrency; upstream remains the
    # authority for those differently-shaped limits.
    "tinyfish": {"limit": 30, "window_s": 60, "source": "docs"},
    # The only platform-served tool is standard single verification, documented at 100/s. Keep
    # the shared key at one quarter of that allowance; BYOK calls bypass this limiter.
    "bounceban": {"limit": 25, "window_s": 1, "source": "docs"},
    # The public allowance is far higher; keep a conservative shared-key pace.
    "zerobounce": {"limit": 25, "window_s": 1, "source": "policy"},
    "datagma": {"limit": 10, "window_s": 1, "source": "docs"},
    # One shared key serves both 5/s enrichment and 1/s search routes. Until smoothing becomes
    # endpoint-aware, protect the stricter search allowance and accept conservative enrichment.
    "prospeo": {"limit": 1, "window_s": 1, "source": "docs"},
    "aiark": {"limit": 5, "window_s": 1, "source": "docs"},
    # Provisional: Wiza publishes 30/min for company enrichment, but not for search or autocomplete.
    # Reuse that ceiling provider-wide because smoothing is not endpoint-aware yet. This spaces
    # sequential platform calls by about 2s; the limiter's bounded wait is not a strict quota gate.
    # Relax this after real 429 evidence, or when smoothing can vary by endpoint. BYOK bypasses it.
    "wiza": {"limit": 30, "window_s": 60, "source": "docs"},
    # The Basic v2 docs set a shared one-request/second default. Several routes are exempt, but
    # provider-wide smoothing cannot express that difference, so shared-key service stays at 1/s.
    "limadata": {"limit": 1, "window_s": 1, "source": "docs"},
    "trestleiq": {"limit": 10, "window_s": 1, "source": "docs"},
    # Development keys allow 100/minute; production keys allow 1,000/minute. Use the lower
    # documented tier until the shared key's environment is verified. Crawl has the same 100/minute
    # ceiling on both tiers, so this provider-wide pace is safe for all four catalog tools.
    "tavily": {"limit": 100, "window_s": 60, "source": "docs"},
    # GET /account reports 50 queries/s for the current shared account. Pace the platform key to
    # that live account allowance; BYOK bypasses this limiter.
    "serper": {"limit": 50, "window_s": 1, "source": "api"},
    "keenable": {"limit": 10, "window_s": 1, "source": "docs"},
    # Olostep publishes 429 guidance but no numeric general API ceiling, and successful live calls
    # returned no rate-limit headers. Smooth the shared key conservatively until the vendor supplies
    # a contract value or production traffic establishes a safer bound. BYOK bypasses this policy.
    "olostep": {"limit": 5, "window_s": 1, "source": "policy"},
    # Deployment allowance supplied for the shared account. Live responses did not include usable
    # rate headers, so keep the configured 500/min ceiling explicit instead of inferring from them.
    "scrapegraphai": {"limit": 500, "window_s": 60, "source": "policy"},
    # Routing-friendly shared-key pace. The 5,000-request/5h rolling allowance is capacity, not a
    # burst rate; encoding it here would make the spacer add 3.6s before every routed attempt.
    "moltsets": {"limit": 10, "window_s": 1, "source": "policy"},
    # The provider publishes 15-25 requests/s by endpoint family. Use the strictest ceiling while
    # smoothing remains provider-wide. All direct tools are BYOK-only today.
    "openmart": {"limit": 15, "window_s": 1, "source": "docs"},
    "sumble": {"limit": 10, "window_s": 1, "source": "docs"},
    "scrubby": {"limit": 25, "window_s": 1, "source": "docs"},
    "leadsforge": {"limit": 120, "window_s": 60, "source": "headers"},
    "leadmagic": {"limit": 300, "window_s": 60, "source": "docs"},
    "crustdata": {"limit": 30, "window_s": 60, "source": "headers"},
    "tikhub": {"limit": 30, "window_s": 1, "source": "docs"},
    "getleadsio": {"limit": 100, "window_s": 60, "source": "docs"},
}


def policy_population(configured_keys: set[str] | None = None) -> list[str]:
    """Every account a policy must exist for: each platform-key slot plus each aggregator."""
    return sorted(set(all_platform_providers()) | {f"overflow:{a}" for a in AGGREGATORS})


# Decided 2026-08-26/28: tikhub is out of overflow scope (429s, auto top-up works, Monid re-shapes
# its responses); scrapecreators is funded, not routed (every aggregator route is ~10× our price).
_NO_OVERFLOW = frozenset({"tikhub", "scrapecreators"})


def default_policy(provider: str, *, has_key: bool) -> CapacityPolicy:
    ctype, funding, source = _KNOWN.get(provider, ("unknown", "unknown", "none"))
    if provider in NO_BALANCE_API and source == "api":
        source = "manual"
    return CapacityPolicy(
        provider=provider, capacity_type=ctype, funding_mode=funding, source=source,
        auto_funding_enabled=funding == "auto_recharge", enabled=has_key,
        overflow_allowed=provider not in _NO_OVERFLOW,
        quota=_QUOTAS.get(provider), rate_limit=_RATE_LIMITS.get(provider),
    )


async def ensure_policies(db: AsyncSession, *, has_key) -> list[str]:
    """Insert a policy row for every account that lacks one; never overwrite a hand-edited row.
    Returns the providers whose imported policy is still `unknown` — the list a person must classify.
    `has_key(provider) -> bool` says whether the env carries a credential for the slot."""
    from sqlalchemy import select
    existing = {row.provider for row in (await db.execute(select(CapacityPolicy))).scalars()}
    unknown: list[str] = []
    for provider in policy_population():
        if provider in existing:
            continue
        pol = default_policy(provider, has_key=bool(has_key(provider)))
        db.add(pol)
        if pol.capacity_type == "unknown" or pol.funding_mode == "unknown":
            unknown.append(provider)
    return unknown


@dataclass
class LatestState:
    """The published per-provider view — the only capacity fact the call path will ever read."""

    provider: str
    remaining: float | None
    unit: str
    observed_at: datetime | None
    confidence: str
    exhausted_until: datetime | None = None
    runway_days: float | None = None  # filled by step C's forecast; None until then
    health: str = "unknown"          # ok | low | exhausted | stale | unknown
    note: str = ""
    rate_limit: dict | None = None   # {"limit", "window_s", "source"} from the policy, for smoothing

    def to_json(self) -> dict:
        return {
            "provider": self.provider, "remaining": self.remaining, "unit": self.unit,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "confidence": self.confidence,
            "exhausted_until": self.exhausted_until.isoformat() if self.exhausted_until else None,
            "runway_days": self.runway_days, "health": self.health, "note": self.note,
            "rate_limit": self.rate_limit,
        }

    @classmethod
    def from_json(cls, d: dict) -> "LatestState":
        def _dt(v):
            return datetime.fromisoformat(v) if v else None
        return cls(provider=d["provider"], remaining=d.get("remaining"), unit=d.get("unit", ""),
                   observed_at=_dt(d.get("observed_at")), confidence=d.get("confidence", "stale"),
                   exhausted_until=_dt(d.get("exhausted_until")), runway_days=d.get("runway_days"),
                   health=d.get("health", "unknown"), note=d.get("note", ""),
                   rate_limit=d.get("rate_limit"))

    def is_exhausted(self, now: datetime | None = None) -> bool:
        if self.exhausted_until is None:
            return False
        return (now or utcnow_naive()) < self.exhausted_until


STALE_AFTER = timedelta(hours=6)


def latest_state(policy: CapacityPolicy, snap: CapacitySnapshot | None,
                 now: datetime | None = None) -> LatestState:
    """Pure: the state one snapshot implies. A missing/failed/old snapshot is `stale`, never
    exhausted — stale must not refuse calls (plan §4.1: blocking fires on confirmed signals only).
    `remaining <= 0` on an exact observation IS a confirmed signal: exhausted until `resets_at`
    when the meter resets, else until the next sweep can prove otherwise (STALE_AFTER)."""
    now = now or utcnow_naive()
    rl = policy.rate_limit
    if snap is None:
        return LatestState(policy.provider, None, "", None, "stale", health="unknown",
                           note="no observation yet", rate_limit=rl)
    if snap.confidence == "informational" and not snap.error:
        old = now - snap.observed_at > STALE_AFTER
        return LatestState(policy.provider, None, snap.unit, snap.observed_at,
                           "stale" if old else "informational", health="stale" if old else "unknown",
                           note=snap.note, rate_limit=rl)
    if snap.error or snap.remaining is None:
        return LatestState(policy.provider, None, snap.unit, snap.observed_at, "stale",
                           health="stale", note=snap.error or snap.note, rate_limit=rl)
    if now - snap.observed_at > STALE_AFTER:
        return LatestState(policy.provider, snap.remaining, snap.unit, snap.observed_at, "stale",
                           health="stale", note="last observation older than 6h", rate_limit=rl)
    if snap.remaining <= 0:
        until = snap.resets_at or (snap.observed_at + STALE_AFTER)
        return LatestState(policy.provider, snap.remaining, snap.unit, snap.observed_at,
                           snap.confidence, exhausted_until=until, health="exhausted",
                           note=snap.note, rate_limit=rl)
    return LatestState(policy.provider, snap.remaining, snap.unit, snap.observed_at,
                       snap.confidence, health="ok", note=snap.note, rate_limit=rl)
