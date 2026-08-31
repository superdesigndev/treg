"""Overflow routes — the rules that decide which `(endpoint, aggregator)` pairs may serve, and the
sync that derives them from the aggregators' catalogs + the verified seed.

Pure except for `apply_sync` (writes the worker-owned `overflowroute` table). The call path reads
the table through an in-process view (step E); nothing here runs on a request.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import CapacityPolicy, OverflowRoute
from ...timeutil import utcnow_naive

MAX_RATIO = 4.0
"""Caller pays the aggregator's real price; a route above this multiple of ours is never enabled."""
VERIFY_MAX_AGE = timedelta(days=7)
FREE_ROUTE_MAX_USD = 0.01
"""A FREE endpoint of ours has no ratio (÷0). Its account still runs dry — tomba's free
`emails.count` threw 155 capacity errors in the reference month — so it may overflow when the
aggregator's price is at most this (disclosed like any other overflow charge)."""
AGGREGATOR_ORDER = ("orthogonal", "monid")
SEED_PATH = Path(__file__).with_name("overflow_seed.json")

# The vendor path-prefix normalizations the mapping run observed (plan §4.3): the aggregator writes
# the base path into the endpoint path where our catalog keeps it on the provider's base URL.
_PREFIXES = ("/v2", "/api", "/v5", "/v1", "/v3")


def norm_path(p: str) -> str:
    p = "/" + re.sub(r"/+", "/", (p or "")).strip("/")
    return re.sub(r"\{[^}]+\}", "{}", p).lower()


def url_key(base: str, path: str) -> tuple[str, str]:
    """(host, full path) with the vendor's base path folded in and {placeholders} normalized."""
    u = urlsplit(base or "")
    return (u.netloc.lower(), norm_path((u.path or "") + "/" + (path or "")))


def our_unit_kind(cost: dict | None) -> str | None:
    """What ONE chargeable event of ours is. `per_result` with `per: N > 1` (Hunter's one credit per
    10 emails, rounded up) is a per-call minimum in practice, so it compares as a call."""
    if not cost:
        return None
    t = cost.get("type")
    if t in ("per_call", "per_success", "free"):
        return "call"
    if t == "per_result":
        return "call" if (cost.get("per") or 1) > 1 else "result"
    return None


def our_event_usd(cost_view: dict | None) -> float | None:
    """USD for one chargeable event, on the same footing as `our_unit_kind`: a per-N-results price
    is quoted per N (the whole credit), not per single row."""
    if not cost_view or cost_view.get("usd") is None:
        return None
    per = cost_view.get("per") or 1
    return cost_view["usd"] * per if (cost_view.get("type") == "per_result" and per > 1) else cost_view["usd"]


def price_ratio(agg_usd: float | None, ours_usd: float | None) -> float | None:
    if agg_usd is None or not ours_usd:
        return None
    return round(agg_usd / ours_usd, 4)


@dataclass(frozen=True)
class Eligibility:
    enabled: bool
    reason: str = ""


def eligible(route: OverflowRoute, *, our_cost: dict | None, platform_eligible: bool,
             policy: CapacityPolicy | None, now: datetime | None = None,
             our_usd: float | None = None) -> Eligibility:
    """The whole enable rule, in one place, in the order a maintainer would ask the questions."""
    now = now or utcnow_naive()
    if not platform_eligible:
        return Eligibility(False, "endpoint not platform-eligible")
    if policy is not None and not policy.overflow_allowed:
        return Eligibility(False, f"policy for {route.provider} disallows overflow")
    kind = our_unit_kind(our_cost)
    if kind is None:
        return Eligibility(False, "our price has no unit")
    if route.agg_unit != kind and not (route.agg_unit == "result" and kind == "call"
                                       and route.single_result):
        return Eligibility(False, f"unit mismatch: ours {kind}, aggregator {route.agg_unit}")
    agg_usd = (route.agg_price_micro or 0) / 1_000_000 if route.agg_price_micro is not None else None
    if our_usd == 0 and agg_usd is not None:
        if agg_usd > FREE_ROUTE_MAX_USD:
            return Eligibility(False, f"free for us, aggregator ${agg_usd:g} > ${FREE_ROUTE_MAX_USD}")
    elif route.ratio is None:
        return Eligibility(False, "no price on one side")
    elif route.ratio > MAX_RATIO:
        return Eligibility(False, f"ratio {route.ratio} > {MAX_RATIO}")
    if route.last_verified_at is None:
        return Eligibility(False, "never verified")
    if now - route.last_verified_at > VERIFY_MAX_AGE:
        return Eligibility(False, f"last verified {route.last_verified_at:%Y-%m-%d}, older than 7 days")
    return Eligibility(True)


def load_seed(path: Path = SEED_PATH) -> list[dict]:
    return json.loads(path.read_text())


def _dt(v) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def match_catalogs(our_endpoints: list[dict], *, orthogonal_apis: list[dict] | None = None,
                   monid_endpoints: list[dict] | None = None, monid_alias: dict[str, str] | None = None) -> list[dict]:
    """Aggregator catalogs → candidate rows, by exact `(host, method, path)` for Orthogonal and by
    `(provider, path)` for Monid (which sometimes writes the vendor's version prefix, sometimes not).
    `our_endpoints` rows: endpoint_id, provider, method, path, base_url. Pure."""
    alias = monid_alias or {}
    orth_index: dict[tuple, tuple[str, dict]] = {}
    for a in orthogonal_apis or []:
        for e in a.get("endpoints", []):
            orth_index[(url_key(a.get("baseUrl") or "", e["path"]), e["method"].upper())] = (a["slug"], e)
    monid_index: dict[tuple, dict] = {}
    for m in monid_endpoints or []:
        prov = alias.get(m["provider"], m["provider"])
        monid_index.setdefault((prov, norm_path(m.get("endpoint") or "")), m)
    out: list[dict] = []
    for ep in our_endpoints:
        key = url_key(ep["base_url"], ep["path"])
        hit = orth_index.get((key, ep["method"].upper()))
        if hit:
            slug, e = hit
            out.append({"endpoint_id": ep["endpoint_id"], "provider": ep["provider"], "method": ep["method"],
                        "path": ep["path"], "aggregator": "orthogonal", "agg_slug": slug, "agg_path": e["path"],
                        "agg_price_usd": _usd(e.get("price")), "agg_unit": "call"})
        m = monid_index.get((ep["provider"], norm_path(ep["path"]))) or monid_index.get((ep["provider"], key[1]))
        if m:
            ptype = ((m.get("price") or {}).get("type") or "").upper()
            out.append({"endpoint_id": ep["endpoint_id"], "provider": ep["provider"], "method": ep["method"],
                        "path": ep["path"], "aggregator": "monid", "agg_slug": m["provider"],
                        "agg_path": m["endpoint"], "agg_price_usd": _usd(m.get("price")),
                        "agg_unit": "result" if ptype == "PER_RESULT" else "call"})
    return out


def _usd(price) -> float | None:
    """'$0.03' | 'dynamic' | {'amount': {'value': .., 'currency': 'USD'}} → USD float or None."""
    if price is None:
        return None
    if isinstance(price, dict):
        amt = price.get("amount") or {}
        if str(amt.get("currency", "USD")).upper() != "USD":
            return None
        return float(amt["value"]) if amt.get("value") is not None else None
    s = str(price).strip()
    if s.startswith("$"):
        try:
            return float(s[1:].replace(",", ""))
        except ValueError:
            return None
    return None


@dataclass
class SyncResult:
    rows: int
    enabled: int
    disabled: dict[str, int]


async def apply_sync(db: AsyncSession, candidates: list[dict], *, catalog, now: datetime | None = None) -> SyncResult:
    """Upsert candidate rows and re-derive `enabled` for every row. A candidate seen before keeps its
    verification stamp unless the candidate carries a newer one; a row absent from `candidates`
    keeps its data but is disabled ("not in the current sync") so a vanished aggregator listing
    stops serving at once. `catalog` is the loaded catalog (cost_view / platform_eligible / by id)."""
    now = now or utcnow_naive()
    policies = {p.provider: p for p in (await db.execute(select(CapacityPolicy))).scalars()}
    existing = {(r.endpoint_id, r.aggregator): r for r in (await db.execute(select(OverflowRoute))).scalars()}
    by_id = {e["id"]: e for e in catalog.endpoints}
    seen: set[tuple[str, str]] = set()
    enabled = 0
    disabled: dict[str, int] = {}
    for c in candidates:
        key = (c["endpoint_id"], c["aggregator"])
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = OverflowRoute(endpoint_id=c["endpoint_id"], aggregator=c["aggregator"], provider=c["provider"],
                                method=c["method"], path=c["path"], agg_slug=c["agg_slug"], agg_path=c["agg_path"])
            db.add(row)
            existing[key] = row
        row.agg_slug, row.agg_path = c["agg_slug"], c["agg_path"]
        row.agg_unit = c.get("agg_unit") or "call"
        usd = c.get("agg_price_usd")
        row.agg_price_micro = int(round(usd * 1_000_000)) if usd is not None else None
        if c.get("single_result") is not None:
            row.single_result = bool(c["single_result"])
        row.matched_at = _dt(c.get("matched_at")) or row.matched_at or now
        v = _dt(c.get("verified_at"))
        if v and (row.last_verified_at is None or v > row.last_verified_at):
            row.last_verified_at = v
        ep = by_id.get(c["endpoint_id"])
        cost = catalog.cost_view(ep.get("cost"), ep.get("provider")) if ep else None
        row.ratio = price_ratio(usd, our_event_usd(cost))
        verdict = eligible(row, our_cost=ep.get("cost") if ep else None,
                           platform_eligible=bool(ep and catalog.platform_eligible(ep)),
                           policy=policies.get(row.provider), now=now,
                           our_usd=(cost or {}).get("usd"))
        row.enabled, row.disabled_reason, row.updated_at = verdict.enabled, verdict.reason, now
        if verdict.enabled:
            enabled += 1
        else:
            disabled[verdict.reason.split(":")[0]] = disabled.get(verdict.reason.split(":")[0], 0) + 1
    for key, row in existing.items():
        if key not in seen and row.enabled:
            row.enabled, row.disabled_reason, row.updated_at = False, "not in the current sync", now
    return SyncResult(len(seen), enabled, disabled)


def route_for(routes: list[OverflowRoute], endpoint_id: str) -> list[OverflowRoute]:
    """Enabled routes for an endpoint in aggregator order — Orthogonal first (plan decision)."""
    mine = [r for r in routes if r.endpoint_id == endpoint_id and r.enabled]
    return sorted(mine, key=lambda r: AGGREGATOR_ORDER.index(r.aggregator) if r.aggregator in AGGREGATOR_ORDER else 99)
