"""Candidate selection and ranking — expected cost per useful result (plan §3), pure."""

from __future__ import annotations

import hashlib
import json
import re

from dataclasses import dataclass, field
from typing import Any

from . import paths as P
from .contracts import Adapter, Contract, adapter_accepts

MAX_ERROR_FALLBACKS = 2
# P(hit|2xx) for a child below the `stats.MIN_HIT_SAMPLES` hit-rate publication floor.
UNMEASURED_HIT_PRIOR = 0.5


def _used_keys(adapter: Adapter) -> set[str]:
    return set(adapter.in_map) | {n for e in (adapter.in_expr or {}).values() for n in re.findall(r"[A-Za-z_]\w*", e)}


def ignored_filters(adapter: Adapter, contract: Contract, identity: dict[str, Any],
                    given: set[str] | frozenset[str] | None = None) -> tuple[str, ...]:
    """Filters the caller supplied that this adapter has no place for — the provider will answer a
    LOOSER question than the one asked. Pure, and knowable before the call, so ranking can use it.

    `given` = the keys the caller actually sent. A contract default fills a filter the caller left
    out (`language: en`), and a default is not a question anyone asked: counting it ranked a child
    with no language field behind one fifty times its price on every call. None = every filter in
    `identity` was sent.

    A filter the adapter sends only through a one-argument transform of it (`dfs_location(country)`)
    is ignored when the transform has no value for what was sent: the provider gets no location,
    which is the same looser question as an adapter with no country field at all.

    `limit` is a page size, not a filter: an adapter without one answers the same question, and its
    price already reflects the rows it bills. The contract defaults it, so counting it ranked every
    limit-less child last, so a free-on-miss role finder fell behind a search that bills its empty
    answers."""
    used = _used_keys(adapter)
    # A filter written into a stand-in's text (`q` "… in Germany") reaches the provider there.
    for k in _stand_in_sent(adapter, identity):
        used |= _carried(contract, k, identity)
    return tuple(k for k in (contract.filters or ()) if k != "limit"
                 and identity.get(k) not in (None, "") and (given is None or k in given)
                 and (k not in used or _unmappable(adapter, k, identity)))


def _unmappable(adapter: Adapter, key: str, identity: dict[str, Any]) -> bool:
    """True when `key` reaches the provider only through `f(key)` / `f(g(key))` expressions and
    every one of them yields nothing for this value."""
    if key in adapter.in_map:
        return False
    exprs = [e.replace(" ", "") for e in (adapter.in_expr or {}).values() if re.search(rf"\b{re.escape(key)}\b", e)]
    chains = []  # each place `key` appears, as the `f(g(key))` call chain wrapping it there
    for e in exprs:
        for m in re.finditer(rf"\b{re.escape(key)}\b", e):
            if e[:m.start()].count("'") % 2:  # a string literal ('country' as a field name), not the key
                continue
            start, end, depth = m.start(), m.end(), 0
            while (call := re.search(r"\w+\($", e[:start])) is not None:
                start, depth = call.start(), depth + 1
            if depth == 0 or e[end:end + depth] != ")" * depth:
                return False  # sent as given somewhere (`obj('x', key)`, `choose(key, …)`)
            chains.append(e[start:end + depth])
    if not chains:
        return False
    try:  # a chain nested in a larger value (`list(obj('country', country_name(country)))`) counts too
        return all(P.evaluate(c, identity) in (None, "", []) for c in chains)
    except Exception:  # noqa: BLE001 - ranking must never crash on an expression
        return False


def _carried(contract: Contract, key: str, identity: dict[str, Any]) -> set[str]:
    """The caller's keys a stand-in `key` built from `identity` holds in its text: those of the
    contract's `stand_in_carries` the text says. A key is said when leaving it out changes the text,
    alone or beside any one other key: a country the location already names ("in Dublin, Ireland")
    is said, since without the location it is the place. A value the text never uses (a list, a
    country code with no name) is not."""
    expr = contract.stand_in.get(key)
    names = [n for n in contract.stand_in_carries.get(key, ()) if identity.get(n) not in (None, "")]
    if not expr or not names:
        return set()
    try:
        def without(*drop):
            return P.evaluate(expr, {**identity, **dict.fromkeys(drop)})
        if P.evaluate(expr, identity) in (None, ""):
            return set()
        base = {k: without(k) for k in names}
        return {n for n in names if base[n] != without() or any(
            without(n, k) != base[k] for k in names if k != n)}
    except Exception:  # noqa: BLE001 - scoping must never crash on an expression
        return set()


def _stand_in_sent(adapter: Adapter, identity: dict[str, Any]) -> set[str]:
    """Stand-in keys this adapter's request carries: ones its matched variant names that the caller
    left empty."""
    stand_in = getattr(adapter, "_stand_in", None) or {}
    variant = set(adapter_accepts(adapter, identity) or ()) if stand_in else set()
    return {k for k in stand_in if k in variant and identity.get(k) in (None, "")}


def unscoped(adapter: Adapter, contract: Contract, identity: dict[str, Any]) -> tuple[str, ...]:
    """The contract's `scoping` keys the caller supplied that this adapter never sends. Unlike an
    ignored filter this is not a looser answer but a different question, so the candidate is
    dropped, not ranked down: a `{title}`-only people search asked for `{company_domain, title}`
    returns the same title-matched strangers for every company, and each one bills as a hit."""
    stand_in = getattr(adapter, "_stand_in", None) or {}
    standing = {k for k in stand_in if identity.get(k) in (None, "")}
    # A key folded into a stand-in's text (`join(q, company_domain)`) scopes only a `q` the caller
    # wrote: on a `q` treg built from a title, a domain in the text is a guess, not a scope. The
    # stand-in key itself is still sent. Text joins skip a non-string, so a list is not carried.
    # Only what this call's upstream request carries: a mapped field outside the matched variant is
    # never sent (`to_upstream`), so an adapter that answers {company_domain, title} through its
    # {company_domain} variant does not scope by title.
    variant = set(adapter_accepts(adapter, identity) or ())
    used = {k for k in adapter.in_map if k in variant or k in (adapter._filter_keys or ())}
    for expr in (adapter.in_expr or {}).values():
        names = set(re.findall(r"[A-Za-z_]\w*", expr))
        if not names & set(stand_in):
            used |= names
        elif names & standing:
            used |= names & standing
        else:
            used |= {n for n in names if identity.get(n) is None or isinstance(identity[n], str)}
    # A stand-in this adapter sends carries the keys the contract says its text holds
    # (`stand_in_carries`: `q` from title and company), each only as a string
    for k in stand_in:
        if k in used and k in standing:
            used |= _carried(contract, k, identity)
    # A free-text key the caller wrote rides only in the variant the adapter matched, or in a key
    # the contract says carries it (`carried_by`: q in title). One that answers {company_domain, q}
    # by {company_domain} alone, or {full_name, q} by the name, drops what q asked for.
    for k in stand_in.keys() - standing:
        if variant & ({k} | set(contract.carried_by.get(k, ()))):
            used.add(k)
        else:
            used.discard(k)
    # Any other scoping key is asked by a key the contract says carries it (a company's domain by
    # its name): the matched variant sending that key answers the same question.
    for k, by in contract.carried_by.items():
        if k not in stand_in and variant & set(by):
            used.add(k)
    return tuple(k for k in contract.scoping if identity.get(k) not in (None, "") and k not in used)


def cost_at(cost_view: dict | None, request: dict | None = None, adapter: Adapter | None = None) -> int | None:
    """Micro-USD this request will cost at its requested size (plan §3, bench 08-27): per-result
    prices × the requested `limit` (default 1 for a lookup); flat per-call/per-success as listed;
    a credit-with-minimum (`per: N`) is one whole unit. None when unpriced."""
    if not cost_view or cost_view.get("usd") is None:
        return None
    usd = float(cost_view["usd"])
    fee = _call_fee_micro(cost_view)
    if adapter and adapter.cost_units:
        units = P.evaluate(adapter.cost_units, request or {})
        if type(units) is not int or units < 0:
            return None
        return int(round(usd * units * 1_000_000)) + fee
    t = cost_view.get("type")
    per = cost_view.get("per") or 1
    if t in ("per_result", "quota_rows"):
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
    return int(round(usd * 1_000_000)) + fee


def _call_fee_micro(cost: dict | None) -> int:
    """An Apify-style `call_fee` (USD) that every run bills on top of its rows, hit or miss.
    Settlement multiplies it by each `call_fee_per` list's length; ranking prices one."""
    fee = (cost or {}).get("call_fee")
    return int(round(float(fee) * 1_000_000)) if isinstance(fee, (int, float)) and fee > 0 else 0


def bills_miss(cost: dict | None) -> bool:
    """Whether a child charges for an answer treg counts as a miss. A per-success or per-result
    price charges for what the provider finds, so an empty answer is free unless `cost.bills_miss`
    says the provider still charges for some misses (a minimum charge on an empty page, the search
    half of a lookup) or a `call_fee` charges every run. Every other priced type bills each call."""
    cost = cost or {}
    if cost.get("type") in ("free", None):
        return False
    if cost.get("type") not in ("per_success", "per_result"):
        return True
    return cost.get("bills_miss") is True or bool(cost.get("call_fee"))


def miss_charge_micro(cost_view: dict | None, request: dict | None = None,
                      adapter: Adapter | None = None, price_micro: int | None = None) -> int:
    """What ranking assumes one miss of this request costs, from the child's priced cost view.
    A `call_fee` alone bills just the run's fee. A per-success `bills_miss` charge is a fraction of
    the price that the provider reports and treg settles, so ranking keeps it free rather than
    charging the whole price for it. A per-result `bills_miss` with `miss_units` bills that many
    rows (CompanyEnrich's one-row minimum on an empty page); without it, the whole request, which
    is what settlement charges when nothing in the answer lowers the hold."""
    cost = cost_view or {}
    if not bills_miss(cost):
        return 0
    if cost.get("type") in ("per_success", "per_result") and (
            cost.get("bills_miss") is not True or cost.get("type") == "per_success"):
        return _call_fee_micro(cost)
    if isinstance(cost.get("miss_units"), int):
        return cost_at(cost_view, {"limit": cost["miss_units"]}) or 0
    if price_micro is None:
        price_micro = cost_at(cost_view, request, adapter) or 0
    return price_micro


@dataclass
class Candidate:
    endpoint: dict
    adapter: Adapter
    variant: tuple[str, ...]
    tier: str                        # tool | credential | anonymous | platform
    price_micro: int | None          # this request, this org (0 on an own key or anonymous route)
    hit_rate: float | None           # P(hit) when published (≥ stats.MIN_HIT_SAMPLES), else None
    ok_rate: float | None
    p50_ms: int | None
    last_ok_days: int | None
    exhausted: bool = False
    note: str = ""
    ignored: tuple[str, ...] = ()     # caller filters this adapter cannot express (ranks it down)
    # What one miss costs (`miss_charge_micro`, or an overflow route's price). None: the full
    # price when the child's cost bills a miss, else 0.
    miss_micro: int | None = None
    p95_ms: int | None = None
    # The short-window verdict (`routing.health`): `slow` asks it after the others of its tier,
    # `failing` is kept only as a probe or when nothing else is left; `ok` changes nothing.
    health: str = "ok"

    @property
    def expected_cost_per_hit(self) -> float:
        """(P(hit) × price + P(miss) × miss charge) / P(hit). Own keys and anonymous routes are
        free. Without a published hit rate, P(hit) is the prior times the ok rate (1.0 while that is
        unmeasured too). A miss is free unless the child's cost bills it (`miss_micro`)."""
        if self.price_micro is None:
            return float("inf")
        if self.tier != "platform":
            return 0.0
        p_hit = self.hit_rate if self.hit_rate is not None else \
            UNMEASURED_HIT_PRIOR * (self.ok_rate if self.ok_rate is not None else 1.0)
        p_hit = max(p_hit, 0.01)
        miss = self.miss_micro if self.miss_micro is not None else \
            (self.price_micro if bills_miss(self.endpoint.get("cost")) else 0)
        return self.price_micro + miss * (1.0 - p_hit) / p_hit

    @property
    def confidence(self) -> str:
        return "measured" if self.hit_rate is not None else ("ok_rate" if self.ok_rate is not None else "unmeasured")

    def view(self) -> dict:
        return {"endpoint_id": self.endpoint["id"], "provider": self.endpoint["provider"], "tier": self.tier,
                "identity": list(self.variant), "price_micro": self.price_micro,
                "expected_cost_per_hit_micro": (None if self.expected_cost_per_hit == float("inf") else int(self.expected_cost_per_hit)),
                "hit_rate": self.hit_rate, "ok_rate": self.ok_rate, "p50_ms": self.p50_ms,
                "confidence": self.confidence, "exhausted": self.exhausted, **({"note": self.note} if self.note else {}),
                **({"health": self.health} if self.health != "ok" else {}),
                **({"ignored_filters": list(self.ignored)} if self.ignored else {})}


@dataclass
class Plan:
    contract: Contract
    identity: dict[str, Any]
    variant: tuple[str, ...]
    candidates: list[Candidate] = field(default_factory=list)   # ranked, callable only
    dropped: list[dict] = field(default_factory=list)            # {endpoint_id, why}
    route_rule: dict[str, str] | None = None   # what ordered it (`route_order`, published rule), for the event
    orders: dict[str, list[str]] = field(default_factory=dict)  # endpoint ids by `default` / `rule` order
    health: dict[str, str] = field(default_factory=dict)  # endpoint id -> short-window verdict, when not ok

    def view(self) -> dict:
        return {"capability": self.contract.capability, "identity_variant": list(self.variant),
                "plan": [c.view() for c in self.candidates], "dropped": self.dropped,
                **({"health": self.health} if self.health else {})}


def sampled_provider_allowed(provider: str, percentages: dict[str, int], *, org_id: int | None,
                             capability: str, identity: dict[str, Any]) -> bool:
    """Stable per question, so retries and plan previews choose the same candidate set."""
    percent = percentages.get(provider)
    if percent is None or percent == 100:
        return True
    if percent == 0:
        return False
    question = json.dumps([provider, org_id, capability, identity], sort_keys=True, separators=(",", ":"), default=str)
    bucket = int.from_bytes(hashlib.sha256(question.encode()).digest()[:8], "big") % 10_000
    return bucket < percent * 100


def rank(candidates: list[Candidate], *, prefer: list[str] | None = None, exclude: list[str] | None = None,
         given: set[str] | frozenset[str] | None = None, derive: dict[str, str] | None = None,
         one_subject: bool = False, caller_prefer: bool = False) -> list[Candidate]:
    """`given` = the identity keys the CALLER actually sent; `derive` = the contract's derive rules.
    `one_subject` (the contract's flag): every variant names the same person, so expected cost per
    hit leads and specificity only breaks ties. Specificity first sent `{linkedin_url, full_name,
    domain}` phone finds to a ~$0.25 name+domain child ahead of LinkedIn children charging a
    fraction of a cent that hit on the same people.
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
        # A team's own credential is the first choice. An approved anonymous route comes next,
        # before a paid platform-key call. Keep this explicit: treating every non-platform tier as
        # "own" would let a future anonymous adapter compete at the team's credential priority.
        tier = 0 if c.tier in ("tool", "credential") else 1 if c.tier == "anonymous" else 2
        pref = prefer.index(c.endpoint["provider"].lower()) if c.endpoint["provider"].lower() in prefer else len(prefer)
        # A provider that USES more of what the caller said answers the question asked: given
        # {company_domain, title}, a title-aware search outranks a cheaper domain-only one that
        # would return the whole company. Price decides among candidates of equal specificity.
        specificity = -covers(c.variant) if given else 0
        # A provider that cannot express a filter the caller SENT answers a looser question, and a
        # non-empty answer to the looser question still counts as a hit — so it must never win on
        # price alone. It stays reachable, just last among equals.
        cost = c.expected_cost_per_hit
        lead = (len(c.ignored), cost, specificity) if one_subject else (specificity, len(c.ignored), cost)
        # A child failing or slow right now is asked after the healthy ones of its tier, whatever
        # order a contract default or a published rule gives it. Only the caller's own prefer
        # (`caller_prefer`) outranks health, and a team's own key is never asked later for it.
        unwell = 0 if c.health == "ok" else 1 if c.health == "slow" else 2
        # A contract default or a published rule orders only children that ask the whole question:
        # one that ignores a sent filter loses its place in it, so it never answers a looser question
        # ahead of one that asks the real one. The caller's own prefer still places it.
        if c.ignored and not caller_prefer:
            pref = len(prefer)
        order = (pref, unwell) if caller_prefer else (unwell, pref)
        return (tier, *order, *lead,
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
        if ad is None or not ad.route:
            continue  # no routing adapter → not a router candidate, silently (still callable via /call/)
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
