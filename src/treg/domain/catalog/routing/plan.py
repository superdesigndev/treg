"""Candidate selection and ranking — expected cost per useful result (plan §3), pure."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

from .contracts import Adapter, Contract, adapter_accepts

MAX_ERROR_FALLBACKS = 2
MIN_HIT_SAMPLES = 50


def ignored_filters(adapter: Adapter, contract: Contract, identity: dict[str, Any]) -> tuple[str, ...]:
    """Filters the caller supplied that this adapter has no place for — the provider will answer a
    LOOSER question than the one asked. Pure, and knowable before the call, so ranking can use it."""
    used = set(adapter.in_map) | {n for e in (adapter.in_expr or {}).values() for n in re.findall(r"[A-Za-z_]\w*", e)}
    return tuple(k for k in (contract.filters or ()) if identity.get(k) not in (None, "") and k not in used)


def cost_at(cost_view: dict | None, request: dict | None = None) -> int | None:
    """Micro-USD this request will cost at its requested size (plan §3, bench 08-27): per-result
    prices × the requested `limit` (default 1 for a lookup); flat per-call/per-success as listed;
    a credit-with-minimum (`per: N`) is one whole unit. None when unpriced."""
    if not cost_view or cost_view.get("usd") is None:
        return None
    usd = float(cost_view["usd"])
    t = cost_view.get("type")
    per = cost_view.get("per") or 1
    if t == "per_result":
        n = 1
        for k in ("limit", "count", "size", "per_page", "num"):
            v = (request or {}).get(k)
            if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
                n = max(1, int(v))
                break
        if per > 1:  # priced per N rows, rounded up to whole units
            usd = usd * per * (-(-n // per))
        else:
            usd = usd * n
    return int(round(usd * 1_000_000))


@dataclass
class Candidate:
    endpoint: dict
    adapter: Adapter
    variant: tuple[str, ...]
    tier: str                        # tool | credential | platform
    price_micro: int | None          # this request, this org (0 on an own key)
    hit_rate: float | None           # P(hit) when known (≥ MIN_HIT_SAMPLES), else None
    ok_rate: float | None
    p50_ms: int | None
    last_ok_days: int | None
    exhausted: bool = False
    note: str = ""
    ignored: tuple[str, ...] = ()     # caller filters this adapter cannot express (ranks it down)

    @property
    def expected_cost_per_hit(self) -> float:
        """price × P(billed) / P(hit). Own keys are free. Unknown rates read as 1.0 (low confidence)."""
        if self.price_micro is None:
            return float("inf")
        if self.tier != "platform":
            return 0.0
        p_hit = self.hit_rate if self.hit_rate is not None else (self.ok_rate if self.ok_rate is not None else 1.0)
        p_hit = max(p_hit, 0.01)
        cost_type = (self.endpoint.get("cost") or {}).get("type")
        p_billed = p_hit if cost_type == "per_success" else 1.0
        return self.price_micro * p_billed / p_hit

    @property
    def confidence(self) -> str:
        return "measured" if self.hit_rate is not None else ("ok_rate" if self.ok_rate is not None else "unmeasured")

    def view(self) -> dict:
        return {"endpoint_id": self.endpoint["id"], "provider": self.endpoint["provider"], "tier": self.tier,
                "identity": list(self.variant), "price_micro": self.price_micro,
                "expected_cost_per_hit_micro": (None if self.expected_cost_per_hit == float("inf") else int(self.expected_cost_per_hit)),
                "hit_rate": self.hit_rate, "ok_rate": self.ok_rate, "p50_ms": self.p50_ms,
                "confidence": self.confidence, "exhausted": self.exhausted, **({"note": self.note} if self.note else {}),
                **({"ignored_filters": list(self.ignored)} if self.ignored else {})}


@dataclass
class Plan:
    contract: Contract
    identity: dict[str, Any]
    variant: tuple[str, ...]
    candidates: list[Candidate] = field(default_factory=list)   # ranked, callable only
    dropped: list[dict] = field(default_factory=list)            # {endpoint_id, why}

    def view(self) -> dict:
        return {"capability": self.contract.capability, "identity_variant": list(self.variant),
                "plan": [c.view() for c in self.candidates], "dropped": self.dropped}


def rank(candidates: list[Candidate], *, prefer: list[str] | None = None, exclude: list[str] | None = None,
         given: set[str] | frozenset[str] | None = None, derive: dict[str, str] | None = None) -> list[Candidate]:
    """`given` = the identity keys the CALLER actually sent; `derive` = the contract's derive rules.
    The specificity tiebreak counts how many of the caller's keys a variant COVERS — a key the
    variant names, or one it was derived from (`first_name`/`last_name` cover a supplied
    `full_name`; `domain` covers a supplied `email`). So `{first_name, last_name, domain}` and
    `{full_name, domain}` are equally specific for a caller who sent a full name and a domain, and
    price decides between hunter and apollo — while a domain-only variant still ranks below both."""
    prefer = [p.lower() for p in prefer or []]
    given = set(given or ())
    derive = derive or {}

    def covers(variant) -> int:
        covered = set(variant) & given
        for v in variant:
            expr = derive.get(v)
            if expr:
                covered |= {g for g in given if re.search(rf"\b{re.escape(g)}\b", expr)}
        return len(covered)
    exclude = {p.lower() for p in exclude or []}
    keep = [c for c in candidates if c.endpoint["provider"].lower() not in exclude and not c.exhausted]

    def key(c: Candidate):
        own = 0 if c.tier != "platform" else 1
        pref = prefer.index(c.endpoint["provider"].lower()) if c.endpoint["provider"].lower() in prefer else len(prefer)
        # A provider that USES more of what the caller said answers the question asked: given
        # {company_domain, title}, a title-aware search outranks a cheaper domain-only one that
        # would return the whole company (live 2026-08-29: a free domain-only provider won and
        # answered 100 unnamed rows). Price decides among candidates of equal specificity.
        specificity = -covers(c.variant) if given else 0
        # A provider that cannot express a filter the caller SENT answers a looser question, and a
        # non-empty answer to the looser question still counts as a hit — so it must never win on
        # price alone (live 2026-08-29: `{q, title, location: London, country: GB}` went to the
        # cheapest candidate, which dropped both geo filters and returned people in Bengaluru and
        # San Francisco). It stays reachable, just last among equals.
        return (own, pref, specificity, len(c.ignored), c.expected_cost_per_hit,
                c.p50_ms if c.p50_ms is not None else 10**9,
                c.last_ok_days if c.last_ok_days is not None else 10**6, c.endpoint["id"])
    return sorted(keep, key=key)


def candidates_for(contract: Contract, endpoints: list[dict], adapters: dict[str, Adapter],
                   identity: dict[str, Any]) -> tuple[list[tuple[dict, Adapter, tuple[str, ...]]], list[dict]]:
    """Endpoints of the capability whose VERIFIED adapter accepts the supplied identity."""
    out, dropped = [], []
    for ep in endpoints:
        if ep.get("status"):
            continue
        ad = adapters.get(ep["id"])
        if ad is None:
            continue  # no adapter → not a router candidate, silently (still callable via /call/)
        if not ad.verified:
            dropped.append({"endpoint_id": ep["id"], "why": f"adapter unverified: {ad.verify_note}"})
            continue
        v = adapter_accepts(ad, identity)
        if v is None:
            # Say what WOULD unlock it — an agent that also knows the LinkedIn URL can add it.
            wants = " | ".join("{" + ", ".join(a) + "}" for a in ad.accepts)
            dropped.append({"endpoint_id": ep["id"], "why": f"needs {wants}"})
            continue
        out.append((ep, ad, v))
    return out, dropped
