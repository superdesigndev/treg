"""Routed endpoints — `POST /call/treg.<capability>` (docs/CAPABILITY-ROUTING-PLAN.md §3–§5).

The router PICKS a child endpoint and runs it through the SAME call use case as any `/call/`:
each attempt is a full `execute_call` on a child `CallContext` whose `call_ref` is
`f"{parent}:r{n}"` — so its hold id, audit row, overflow rung, settle and cancellation
compensation are the ordinary ones, untouched. The parent assembles the answer: the contract's
core `output` (via the child's adapter), the child's `raw` body, and `_treg: {served_by, tried}`.

Fallback follows the overflow rules: on an ERROR (our 5xx/503, a vendor 5xx/429/402) the next
candidate is tried, at most two extra, idempotent contracts only; a caller-caused refusal (4xx)
stops at once — it would be the same 4xx everywhere. Child-local treg authorization failures and
platform vendor 401/403 responses are errors because another child may work. A MISS (2xx,
`adapter.miss`) stops unless the
caller turned the waterfall off (`X-Treg-Route-Waterfall: 0`). The waterfall is ON by default —
the endpoint's job is to find the thing — bounded by `X-Treg-Route-Max-Cost` (default $1.00).
"""

from __future__ import annotations


import json
import re
import logging
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from ... import audit
from ...config import get_settings
from ...infra.db import session_maker
from ...domain.capacity.view import view as capacity_view
from ...domain.catalog import stats as endpoint_stats
from ...domain.catalog import store as catalog_store
from ...domain.catalog.routing.contracts import canonical_identity
from ...domain.catalog.routing.plan import (
    MAX_ERROR_FALLBACKS, Candidate, Plan, candidates_for, cost_at, ignored_filters, rank,
)
from .resolve import _host_of, _marketplace_secret
from .types import CallContext, CallFailure, GatewayFailed, ResolutionFailed, UpstreamResponse

log = logging.getLogger("treg.route")
_endpoint_observation_reader: endpoint_stats.EndpointObservationReader | None = None


def configure_endpoint_observation_reader(reader: endpoint_stats.EndpointObservationReader) -> None:
    """Bind bootstrap's shared process cache to routed planning."""
    global _endpoint_observation_reader
    _endpoint_observation_reader = reader


def clear_endpoint_observation_reader(reader: endpoint_stats.EndpointObservationReader) -> None:
    """Unbind only the reader owned by the lifespan that is stopping."""
    global _endpoint_observation_reader
    if _endpoint_observation_reader is reader:
        _endpoint_observation_reader = None


async def _observed_stats(endpoint_ids: list[str]) -> endpoint_stats.ObservationSnapshot:
    """Read advisory routing evidence without making the request wait for its DB aggregate."""
    reader = _endpoint_observation_reader
    if reader is None:
        return {}
    try:
        return await reader.get_many(endpoint_ids)
    except Exception:  # noqa: BLE001 - routing evidence always degrades to deterministic ranking
        log.warning("endpoint stats unavailable for routed plan", exc_info=True)
        return {}

WATERFALL_HEADER = "x-treg-route-waterfall"
MAX_COST_HEADER = "x-treg-route-max-cost"
PREFER_HEADER = "x-treg-route-prefer"
EXCLUDE_HEADER = "x-treg-route-exclude"
MIN_RESULTS_HEADER = "x-treg-route-min-results"
MERGE_HEADER = "x-treg-route-merge"
_DROP_FROM_CHILD = frozenset({b"content-length", b"content-type", b"transfer-encoding", b"idempotency-key",
                              b"x-treg-route-waterfall", b"x-treg-route-max-cost", b"x-treg-route-prefer",
                              b"x-treg-route-exclude", b"x-treg-route-min-results",
                              b"x-treg-route-merge", b"host"})
_CALLER_FAULT = frozenset({400, 401, 403, 404, 405, 409, 422})
_CANDIDATE_LOCAL_FAILURES = frozenset({"tool_access_denied", "policy_denied", "capability_pinned"})
_GLOBAL_REFUSALS = frozenset({"insufficient_balance", "tag_spend_cap_reached",
                              "platform_daily_cap_reached", "daily_cap_reached"})


MAX_WEAK_FALLBACKS = 2   # extra providers asked after a thin-but-real answer (see min_results)
CHEAP_RETRY_MICRO = 10_000  # ≤ 1¢: a per_call provider cheap enough to be asked after another's 4xx


def _free_on_failure(cand: Candidate) -> bool:
    """A candidate worth asking after another provider's 4xx: it bills nothing for a rejected request
    (per_success pricing, a free endpoint, the org's own key) or so little (≤ 1¢ per call) that a
    repeated mistake costs less than failing the caller — scrapers answer 400 for their own outages."""
    if cand.tier == "credential" or not cand.price_micro:
        return True
    if (cand.endpoint.get("cost") or {}).get("type") == "per_success":
        return True
    return cand.price_micro <= CHEAP_RETRY_MICRO


DEFAULT_MAX_COST_MICRO = 1_000_000  # $1.00 per routed call unless the caller says otherwise — a runaway guard, not a budget


@dataclass
class RouteOptions:
    waterfall: bool = True
    max_cost_micro: int | None = DEFAULT_MAX_COST_MICRO
    prefer: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    min_results: int = 1     # a hit with fewer rows than this is WEAK: keep looking, keep the best
    merge: bool = False      # union the rows of every attempt that returned some (list answers only)

    @classmethod
    def from_headers(cls, get, default_max_cost_micro: int | None = None) -> "RouteOptions":
        def _list(v):
            return [p.strip() for p in (v or "").split(",") if p.strip()]
        mc = get(MAX_COST_HEADER)
        try:
            max_cost = int(round(float(mc) * 1_000_000)) if mc else (
                default_max_cost_micro if default_max_cost_micro is not None else DEFAULT_MAX_COST_MICRO)
        except ValueError:
            raise ResolutionFailed("catalog_parameter_invalid", status_code=400,
                                   detail=f"{MAX_COST_HEADER} must be a USD number, got {mc!r}")
        wf = str(get(WATERFALL_HEADER) or "").strip().lower()
        try:
            mr = max(1, int(get(MIN_RESULTS_HEADER) or 1))
        except ValueError:
            raise ResolutionFailed("catalog_parameter_invalid", status_code=400,
                                   detail=f"{MIN_RESULTS_HEADER} must be a whole number, got {get(MIN_RESULTS_HEADER)!r}")
        mg = str(get(MERGE_HEADER) or "").strip().lower()
        return cls(waterfall=wf not in ("0", "false", "no", "off"),
                   max_cost_micro=max_cost, prefer=_list(get(PREFER_HEADER)), exclude=_list(get(EXCLUDE_HEADER)),
                   min_results=mr, merge=mg in ("1", "true", "yes", "on"))


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def stream(self):
        yield self._data

    async def read(self) -> bytes:
        return self._data


@dataclass
class Attempt:
    endpoint_id: str
    provider: str
    outcome: str            # hit | miss | error | skipped
    status: int | None
    charged_micro: int
    detail: str = ""
    ignored: tuple[str, ...] = ()  # filters the caller sent that this provider's adapter has no place for

    def view(self) -> dict:
        return {"endpoint_id": self.endpoint_id, "provider": self.provider, "outcome": self.outcome,
                "status": self.status, "charged_micro": self.charged_micro, **({"detail": self.detail} if self.detail else {}),
                **({"ignored_filters": list(self.ignored)} if self.ignored else {})}


def _list_field(contract) -> str | None:
    """The contract's ONE list-shaped required output (`people`, `companies`). Merging only makes
    sense for these: two answers to "this person's email" is a conflict, not a union."""
    for k in contract.required_output:
        if (contract.output.get(k) or {}).get("type") == "list":
            return k
    return None


def _norm_url(v: str) -> str:
    """Scheme, `www.` and trailing slashes are noise a provider adds or omits at will — two rows for
    one person differ by exactly that (`https://linkedin.com/in/ada` vs `www.linkedin.com/in/Ada/`)."""
    u = v.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def _row_key(row) -> str:
    """Dedup across PROVIDER-NATIVE rows, which share no schema. A profile URL is the only value
    two providers reliably agree on; a normalised name is the fallback, and a row with neither is
    kept (dropping it would silently lose an answer we already paid for)."""
    if not isinstance(row, dict):
        return ""
    for path in ("linkedin_url", "profileUrl", "url", "linkedinUrl", "profile_url"):
        v = row.get(path)
        if isinstance(v, str) and v.strip():
            return _norm_url(v)
    urls = row.get("URLs")
    if isinstance(urls, dict) and isinstance(urls.get("linkedin"), str):
        return _norm_url(urls["linkedin"])
    name = row.get("fullName") or row.get("full_name") or row.get("name") or " ".join(
        str(row.get(k) or "") for k in ("firstname", "lastname")).strip()
    return re.sub(r"[^a-z0-9]+", "", str(name).lower()) if name else ""


def _merge_rows(field: str, answers: list[tuple], limit: int | None):
    """Union in ATTEMPT order — the ranking of the provider that answered first leads, later
    providers append what they add. The caller already paid for every one of these rows
    (`spent` sums every attempt), so discarding them is pure waste."""
    seen, out = set(), []
    for _cand, _doc, core, _raw in answers:
        for r in (core.get(field) or []):
            k = _row_key(r)
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            out.append(r)
    return out[:limit] if limit else out


async def build_plan(ep: dict, identity_given: dict, caller, options: RouteOptions) -> Plan:
    """Candidates for this org: adapters that accept the identity, own keys first, exhausted
    providers dropped, ranked by expected cost per hit. Reads only; nothing reserved."""
    cat = catalog_store.load()
    contract = cat.contracts.get(ep["capability"])
    if contract is None:
        raise ResolutionFailed("target_not_found", status_code=404, detail=f"{ep['id']} has no contract")
    identity, variant = canonical_identity(contract, identity_given)
    if variant is None:
        raise ResolutionFailed("route_no_candidate", status_code=422, detail={
            "error": "identity_incomplete", "endpoint_id": ep["id"],
            "message": f"{ep['id']} needs exactly one identity variant in the JSON body: "
                       + " | ".join("{" + ", ".join(v) + "}" for v in contract.identity),
            "variants": [list(v) for v in contract.identity]})
    raw, dropped = candidates_for(contract, cat.for_capability(ep["capability"]), cat.adapters, identity)
    ids = [e["id"] for e, _, _ in raw]
    stats = await _observed_stats(ids)
    own: set[str] = set()
    own_tools: set[str] = set()
    if ids:
        from sqlalchemy import select as _select
        from ... import oauth_providers
        from ...models import Tool
        async with session_maker() as db:
            services = {e["provider"] for e, _, _ in raw}
            if caller.org_id is not None:
                # tier 1: a tool the team REGISTERED for the provider's host (their credential,
                # their ACLs) — the ladder would pick it anyway; the ranking must know it is free.
                hosts = {}
                for service in services:
                    prov = oauth_providers.get(service)
                    if prov is not None and prov.base_url:
                        hosts.setdefault(_host_of(prov.base_url), service)
                if hosts:
                    tools = (await db.execute(_select(Tool.host).where(Tool.org_id == caller.org_id))).scalars().all()
                    own_tools = {hosts[h] for h in tools if h in hosts}
            for service in services:
                if service in own_tools:
                    continue
                if caller.org_id is not None and await _marketplace_secret(service, caller.org_id, db) is not None:
                    own.add(service)
    cands: list[Candidate] = []
    for e, ad, v in raw:
        st = stats.get(e["id"]) or {}
        tier = "tool" if e["provider"] in own_tools else "credential" if e["provider"] in own else "platform"
        cv = cat.cost_view(e.get("cost"), e["provider"])
        price = 0 if tier != "platform" else cost_at(cv, identity)
        c = Candidate(endpoint=e, adapter=ad, variant=v, tier=tier, price_micro=price, hit_rate=st.get("hit_rate"),
                      ok_rate=st.get("ok_rate"), p50_ms=st.get("p50_ms"), last_ok_days=st.get("last_ok_days"),
                      exhausted=(tier == "platform" and capacity_view.is_exhausted(e["provider"])),
                      ignored=ignored_filters(ad, contract, identity))
        if tier == "platform" and not cat.platform_eligible(e):
            dropped.append({"endpoint_id": e["id"], "why": "not platform-eligible and no own key"})
            continue
        if tier == "platform" and not get_settings().platform_key_for(e["provider"]):
            # priceable, but this deployment holds no key for the provider: the child would answer
            # "no credential" (a 404 the router must not treat as the caller's fault). Live 2026-08-28.
            dropped.append({"endpoint_id": e["id"], "why": f"no {e['provider']} key on this deployment and no own key"})
            continue
        if c.exhausted:
            dropped.append({"endpoint_id": e["id"], "why": "treg's account for this provider is exhausted right now"})
        cands.append(c)
    return Plan(contract=contract, identity=identity, variant=variant,
                candidates=rank(cands, prefer=options.prefer, exclude=options.exclude,
                                given={k for k, v in (identity_given or {}).items() if v not in (None, "")},
                                derive=contract.derive), dropped=dropped)


def _child_input(parent, ep: dict, query: dict[str, str], body: dict) -> object:
    from .types import CallInput
    has_body = ep["method"] in ("POST", "PUT", "PATCH") and body is not None
    payload = json.dumps(body).encode() if has_body else b""
    headers = [(k, v) for k, v in parent.input.raw_headers if k.lower() not in _DROP_FROM_CHILD]
    if has_body:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    items = tuple(query.items())
    return CallInput(method=ep["method"], raw_rest=ep["id"], raw_headers=tuple(headers), query_items=items,
                     raw_query=urlencode(items), body=_Bytes(payload), caller=parent.input.caller,
                     client_ip=parent.input.client_ip, catalog_only=parent.input.catalog_only)


async def _read(response: UpstreamResponse) -> bytes:
    chunks = []
    async for chunk in response.body_stream:
        chunks.append(chunk)
    await response.close()
    return b"".join(chunks)


def _header(response: UpstreamResponse, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for k, v in response.raw_headers:
        if k.lower() == wanted:
            return v.decode("latin-1")
    return None


async def run_routed(parent: CallContext, ep: dict, body_bytes: bytes, get_header, upstream_client: httpx.AsyncClient,
                     execute_child, *, audit_client: str = "") -> tuple[UpstreamResponse, int]:
    """Execute a routed call under `parent`. Returns (the assembled response, total charged)."""
    try:
        given = json.loads(body_bytes or b"{}")
    except ValueError:
        raise ResolutionFailed("catalog_parameter_invalid", status_code=400, detail=f"{ep['id']} expects a JSON object body")
    if not isinstance(given, dict):
        raise ResolutionFailed("catalog_parameter_invalid", status_code=400, detail=f"{ep['id']} expects a JSON object body")
    contract = catalog_store.load().contracts.get(ep["capability"])
    options = RouteOptions.from_headers(
        get_header, int(round(contract.default_max_cost_usd * 1_000_000)) if contract and contract.default_max_cost_usd else None)
    plan = await build_plan(ep, given, parent.input.caller, options)
    if not plan.candidates:
        raise ResolutionFailed("route_no_candidate", status_code=422 if not plan.dropped else 503, detail={
            "error": "no_route_candidate", "endpoint_id": ep["id"], "identity_variant": list(plan.variant),
            "dropped": plan.dropped,
            "message": f"no provider can serve {ep['id']} for this identity right now"})
    first = plan.candidates[0]
    if options.max_cost_micro is not None and (first.price_micro or 0) > options.max_cost_micro:
        raise ResolutionFailed("route_max_cost", status_code=402, detail={
            "error": "route_max_cost", "endpoint_id": ep["id"], "max_cost_micro": options.max_cost_micro,
            "cheapest_micro": first.price_micro, "plan": plan.view()["plan"],
            "message": f"the cheapest candidate ({first.endpoint['id']}) costs more than {MAX_COST_HEADER}"})
    tried: list[Attempt] = []
    spent = 0
    errors = 0
    rejected_by: set[str] = set()  # providers that answered a vendor 4xx
    winner: tuple[Candidate, dict, dict, bytes] | None = None
    answers: list[tuple] = []      # every attempt that returned rows, for X-Treg-Route-Merge
    weak_hits = 0
    best: tuple[int, tuple[Candidate, dict, dict, bytes]] | None = None   # best WEAK answer seen
    for n, cand in enumerate(plan.candidates):
        if options.max_cost_micro is not None and spent + (cand.price_micro or 0) > options.max_cost_micro:
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped", None, 0, "would exceed max cost"))
            continue
        if rejected_by and (cand.endpoint["provider"] in rejected_by or not _free_on_failure(cand)):
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped", None, 0,
                                 "provider already rejected the request" if cand.endpoint["provider"] in rejected_by
                                 else "not retried on a paid provider (> 1¢/call) after a vendor 4xx"))
            continue
        query, body = cand.adapter.to_upstream(plan.identity, cand.variant)
        # A filter the caller sent that this adapter never mentions is silently NOT applied — say so
        # on the attempt (live 2026-08-29: `country: fr` reached icypeas as nothing, rows came from
        # anywhere; the bench had post-filtered in the agent). Computed at planning time, where it
        # also ranks the candidate down.
        ignored = cand.ignored
        child = CallContext(input=_child_input(parent, cand.endpoint, query, body), call_ref=f"{parent.call_ref}:r{n}", meta=parent.meta)
        try:
            response = await execute_child(child, upstream_client)
        except CallFailure as exc:
            if exc.kind in _GLOBAL_REFUSALS or (
                exc.status_code in _CALLER_FAULT and exc.kind not in _CANDIDATE_LOCAL_FAILURES
            ):
                raise  # the same refusal everywhere; nothing to fall back to
            errors += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", exc.status_code, 0, str(exc.detail)[:120]))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        raw = await _read(response)
        charged = int(_header(response, "X-Treg-Cost-Micro") or 0)
        spent += charged
        platform_auth_failure = cand.tier == "platform" and response.status in (401, 403)
        if (400 <= response.status < 500 and response.status not in (402, 408, 429)
                and not platform_auth_failure):
            # The vendor rejected the REQUEST. Usually the caller's mistake and the same answer
            # everywhere — but a scraper's "Request failed. Please retry" is also a 400 (tikhub,
            # live 2026-08-28), so the waterfall goes on to providers that are FREE ON FAILURE
            # (per_success / free / ≤ 1¢ per call, another provider, the usual error bound). A dearer
            # paid-per-call provider is never asked to bill the same mistake twice (plan §4).
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", response.status, charged, raw[:120].decode("utf-8", "replace")))
            rejected_by.add(cand.endpoint["provider"])
            errors += 1
            if errors <= MAX_ERROR_FALLBACKS and plan.contract.idempotent and any(
                    _free_on_failure(c) and c.endpoint["provider"] not in rejected_by for c in plan.candidates[n + 1:]):
                continue
            _audit_parent(parent, ep, response.status, spent, audit_client)
            raise ResolutionFailed("route_caller_fault", status_code=response.status, detail={
                "error": "route_caller_fault", "endpoint_id": ep["id"], "served_by": cand.endpoint["id"],
                "tried": [t.view() for t in tried], "charged_micro": spent,
                "message": f"{cand.endpoint['id']} rejected the request ({response.status}); "
                           + ("not retried elsewhere" if len(rejected_by) == 1 else "every free-on-failure provider rejected it too"),
                "provider_response": raw[:600].decode("utf-8", "replace")})
        if not 200 <= response.status < 300:
            errors += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", response.status, charged, raw[:120].decode("utf-8", "replace")))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        core = cand.adapter.from_upstream(doc) if doc is not None else {}
        # A miss is what the adapter's predicate says — OR a 2xx whose body does not carry the
        # contract's required core (a null `result` under a 200, an error task inside a 20000
        # envelope): the caller asked for the field and did not get it (live 2026-08-28: dataforseo's
        # yahoo task returned `result: null` and was counted a hit).
        empty_core = any(core.get(k) in (None, "", [], {}) for k in plan.contract.required_output)
        if doc is None or cand.adapter.is_miss(doc) or empty_core:
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "miss", response.status, charged, ignored=ignored))
            if options.waterfall:
                continue
            winner = (cand, doc if doc is not None else {}, {}, raw)
            break
        # A HIT that answers thinly is not the end of the search. `min_results` lets the caller say
        # how many rows make an answer; below it the waterfall keeps going and the best result so
        # far is kept, so a weak first provider can still win if nothing better turns up. Without
        # this the router stops at the first non-empty body — three rows of the wrong people end a
        # search that a later provider would have answered (bench 2026-08-29, recruiting).
        n_rows = max((len(v) for v in core.values() if isinstance(v, list)), default=1)
        weak = options.waterfall and n_rows < options.min_results
        tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "weak" if weak else "hit",
                             response.status, charged, f"{n_rows} < min_results {options.min_results}" if weak else "",
                             ignored=ignored))
        if weak:
            if best is None or n_rows > best[0]:
                best = (n_rows, (cand, doc, core, raw))
            answers.append((cand, doc, core, raw))
            weak_hits += 1
            # Bounded like the error fallback, and for the same reason: a brief whose honest answer
            # IS one or two people (a lookup — "who runs engineering at X") never clears the bar, so
            # an unbounded rule walks the whole paid ladder on every such call. Measured on the
            # bench's deterministic set, unbounded cost 12.7x ($1.76 -> $22.35 over 28 queries) for
            # answers that were already correct.
            if weak_hits > MAX_WEAK_FALLBACKS:
                break
            continue
        answers.append((cand, doc, core, raw))
        winner = (cand, doc, core, raw)
        break
    if winner is None and best is not None:
        winner = best[1]          # nobody cleared min_results — the fullest answer we paid for wins
    if winner is None:
        outcome = "miss" if tried and all(t.outcome in ("miss", "skipped", "weak") for t in tried) else "error"
        if outcome == "miss":
            last = next(t for t in reversed(tried) if t.outcome == "miss")
            body_out = {"output": {k: None for k in plan.contract.output}, "raw": None,
                        "_treg": {"served_by": None, "outcome": "miss", "tried": [t.view() for t in tried], "charged_micro": spent,
                                  **({"dropped": plan.dropped} if plan.dropped else {})}}
            _audit_parent(parent, ep, 200, spent, audit_client)
            return _json(body_out, 200, {"X-Treg-Providers-Tried": ",".join(t.provider for t in tried), "X-Treg-Route-Outcome": "miss"}), spent
        _audit_parent(parent, ep, 502, spent, audit_client)
        raise GatewayFailed("route_failed", status_code=502, detail={
            "error": "route_failed", "endpoint_id": ep["id"], "tried": [t.view() for t in tried], "charged_micro": spent,
            "dropped": plan.dropped,
            "message": f"every candidate for {ep['id']} failed; nothing useful was charged" if spent == 0 else
                       f"every candidate for {ep['id']} failed"})
    cand, doc, output, raw = winner
    served = cand.endpoint["id"]
    merged_from: list[str] = []
    if options.merge and len(answers) > 1:
        # A LIST answer is the only shape a union makes sense for, and the caller has already been
        # charged for every attempt (`charged_micro` is the running sum) — so without this the
        # router throws away rows the team paid for. Bench 2026-08-29: a `people.search` that fell
        # through returned the fullest SINGLE provider's rows, never icypeas' 5 plus exa's 10.
        field = _list_field(plan.contract)
        if field:
            rows = _merge_rows(field, answers, plan.identity.get("limit"))
            if len(rows) > len(output.get(field) or []):
                output = {**output, field: rows}
                merged_from = [c.endpoint["id"] for c, _, core_i, _ in answers if (core_i.get(field) or [])]
                served = merged_from[0]
    # The rows answer a LOOSER question than the caller asked when the winner could not express a
    # filter. It is on the attempt, but no caller reads `tried[]` — say it where the answer is, and
    # on a header, or the agent post-filters nothing and never knows why the geography is wrong.
    body_out = {"output": output or {k: None for k in plan.contract.output}, "raw": doc,
                "_treg": {"served_by": served, "provider": cand.endpoint["provider"], "tier": cand.tier,
                          **({"merged_from": merged_from} if merged_from else {}),
                          "outcome": tried[-1].outcome, "tried": [t.view() for t in tried], "charged_micro": spent,
                          **({"ignored_filters": list(cand.ignored)} if cand.ignored else {}),
                          **({"dropped": plan.dropped} if plan.dropped else {})}}
    _audit_parent(parent, ep, 200, spent, audit_client)
    return _json(body_out, 200, {"X-Treg-Served-By": served, "X-Treg-Providers-Tried": ",".join(t.provider for t in tried),
                                 **({"X-Treg-Merged-From": ",".join(merged_from)} if merged_from else {}),
                                 **({"X-Treg-Ignored-Filters": ",".join(cand.ignored)} if cand.ignored else {}),
                                 "X-Treg-Route-Outcome": tried[-1].outcome}), spent


def _audit_parent(parent: CallContext, ep: dict, status: int, charged: int, client: str) -> None:
    c = parent.input.caller
    audit.record_call(org_id=c.org_id, user_email=c.email, tool_name=ep["id"], method="POST", path=ep["path"],
                      status_code=status, client=client,
                      telemetry={"call_ref": parent.call_ref, "endpoint_id": ep["id"], "provider": "treg",
                                 "credential_tier": "routed", "cost_charged_micro": charged})


def _json(value, status: int, headers: dict[str, str]) -> UpstreamResponse:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    async def _one():
        yield body

    async def _closed():
        return None
    raw = [(b"content-length", str(len(body)).encode()), (b"content-type", b"application/json")]
    raw += [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return UpstreamResponse(status, tuple(raw), _one(), _closed)
