"""Routed endpoints — `POST /call/treg.<capability>`.

The router PICKS a child endpoint and runs it through the SAME call use case as any `/call/`:
each attempt is a full `execute_call` on a child `CallContext` whose `call_ref` is
`f"{parent}:r{n}"` — so its hold id, audit row, overflow rung, settle and cancellation
compensation are the ordinary ones, untouched. The parent assembles the answer: the contract's
core `output` (via the child's adapter), the child's `raw` body, and `_treg: {served_by, tried}`.

Fallback follows the overflow rules: on an ERROR (our 5xx/503, a vendor 5xx/429/402) the next
candidate is tried, at most two extra, idempotent contracts only; a caller-caused refusal (4xx)
stops at once — it would be the same 4xx everywhere — unless the endpoint's YAML declares that
status as its "no result" answer (`miss: {status: 404}`), which is a MISS. A declared
`not_found:` (the target itself does not exist) ends the call with that answer. Child-local treg authorization failures and
platform vendor 401/403 responses are errors because another child may work. A MISS (2xx,
`adapter.miss`) tries the next candidate: the waterfall is ON by default (the endpoint's job is
to find the thing), and each attempt settles at its own price. `X-Treg-Route-Waterfall: 0` stops
at the first miss. `X-Treg-Route-Max-Cost` (default $1.00) bounds the call: a candidate is started
only while what was already charged plus its catalog estimate fits under it.

`X-Treg-Route-Verify` asks for one more call after a hit, the contract's `check` (an email find
checked by `treg.people.email.verify`). It is its own linked child `:v` with its own hold and
verdict, reported in `_treg.verification`; the find's answer is not altered. A check still pending
after the routed async wait keeps `pending: true`, its `call_id`, and the child's `async` poll
descriptor (`task_id` + poll endpoint) so the caller can resume; `cost_micro` is then the hold.

When the caller sent a domain and the hit carries an email, `_treg.domain_match` (and
`X-Treg-Domain-Match`) says whether the mailbox domain matches. A LinkedIn-only child that returns
a former employer still counts as a hit and is billed; the flag discloses the mismatch.

When the caller sent `limit` and the serving child (or a merge) returns more rows, every row stays
and `_treg.limit_exceeded` / `X-Treg-Limit-Exceeded` say so — paid rows are never trimmed.

`X-Treg-Route-Confirm` makes a hit count only once a second provider returns the same value of the
contract's `confirm` field: the waterfall goes on after an unconfirmed hit, each attempt at its own
price. The first provider's answer is returned either way, reported in `_treg.confirmation`; when
no second provider agrees, it comes back marked unconfirmed rather than as a miss.

`X-Treg-Route-Decisive` makes an answer count only when its verdict word is one the contract's
`verdict.decisive` lists (an email check: valid or invalid). Any other word (unknown, catch-all,
risky) or a failed attempt sends the call on, at most two more providers, each at its own price;
the providers left are then asked in order of measured verdict quality (domain/catalog/routing/
quality.py) where it is known, in their price order where it is not. When none decides, the first
undecided answer comes back, reported in `_treg.decision`.

`X-Treg-Route-Quality` asks the whole plan in order of measured answer quality for the identity
sent (the same quality file, sliced by identity variant), the unrated behind in price order; it is
reported in `_treg.ordered_by`. `X-Treg-Route-Upgrade: <key>` re-asks this endpoint by that key
alone when the hit carries it (an email's answer naming a LinkedIn URL), as a linked routed child
`:u` ordered by quality; its answer replaces the first only when it is a hit about the same person,
reported in `_treg.upgrade`. Both are opt-in.
"""

from __future__ import annotations


import asyncio
import json
import re
import logging
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from ... import audit, jsontext
from .. import call_events
from ...config import get_settings
from ...infra.db import session_maker
from ...domain import asynctasks as asynctasks_rules
from ...domain.capacity.routes_view import view as overflow_routes_view
from ...domain.capacity.view import view as capacity_view
from ...domain.capacity.marks import spends_balance
from ...domain.capacity.signatures import classify as classify_capacity, is_exhausting
from ...domain import plugins
from ...domain import money as ledger
from ...domain.catalog import outcome as catalog_outcome
from ...domain.catalog import results as catalog_results
from ...domain.catalog import stats as endpoint_stats
from ...domain.catalog import store as catalog_store
from ...domain.catalog.routing import paths as P
from ...domain.catalog.routing import health as health_rules
from ...domain.catalog.routing import quality as route_quality
from ...domain.catalog.schema.v2 import is_v2
from ...domain.catalog.routing.contracts import (canonical_identity, default_prefer, declared_miss, declared_not_found,
                                                 flags_off, invalid_identity, miss_status)
from ...domain.catalog.routing.plan import (
    MAX_ERROR_FALLBACKS, Candidate, Plan, candidates_for, cost_at, ignored_filters, miss_charge_micro, rank,
    sampled_provider_allowed, unscoped,
)
from ... import oauth_providers
from ... import sandbox as demo_sandbox
from .. import asynctasks as async_task_app
from . import admission, async_bridge, keyslots
from . import route_health
from . import route_rules as route_rules_app
from . import cost as call_cost
from .intake import _tag_telemetry
from .resolve import _anonymous_offer, _anonymous_refusal, _host_of, _marketplace_secret
from .settle import close_deferred
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


_route_quality_reader: route_quality.RouteQualityReader | None = None


def configure_route_quality_reader(reader: route_quality.RouteQualityReader | None) -> None:
    """Bind the measured verdict quality `X-Treg-Route-Decisive` orders by; None = price order."""
    global _route_quality_reader
    _route_quality_reader = reader


async def _route_quality(capability: str) -> route_quality.Snapshot | None:
    if _route_quality_reader is None:
        return None
    try:
        return await _route_quality_reader.get(capability)
    except Exception:  # quality only orders the fallback; no data keeps price order
        log.warning("route quality unavailable", exc_info=True)
        return None


async def _observed_stats(endpoint_ids: list[str]) -> endpoint_stats.ObservationSnapshot:
    """Read advisory routing evidence without making the request wait for its DB aggregate."""
    reader = _endpoint_observation_reader
    if reader is None:
        return {}
    try:
        return await reader.get_many(endpoint_ids)
    except Exception:
        log.warning("endpoint stats unavailable for routed plan", exc_info=True)
        return {}

WATERFALL_HEADER = "x-treg-route-waterfall"
MAX_COST_HEADER = "x-treg-route-max-cost"
PREFER_HEADER = "x-treg-route-prefer"
EXCLUDE_HEADER = "x-treg-route-exclude"
MIN_RESULTS_HEADER = "x-treg-route-min-results"
MERGE_HEADER = "x-treg-route-merge"
STRICT_FILTERS_HEADER = "x-treg-route-strict-filters"
VERIFY_HEADER = "x-treg-route-verify"
CONFIRM_HEADER = "x-treg-route-confirm"
DECISIVE_HEADER = "x-treg-route-decisive"
QUALITY_HEADER = "x-treg-route-quality"
UPGRADE_HEADER = "x-treg-route-upgrade"
COMPLETE_HEADER = "x-treg-route-complete"
_DROP_FROM_CHILD = frozenset({b"content-length", b"content-type", b"transfer-encoding", b"idempotency-key",
                              b"x-treg-route-waterfall", b"x-treg-route-max-cost", b"x-treg-route-prefer",
                              b"x-treg-route-exclude", b"x-treg-route-min-results",
                              b"x-treg-route-merge", b"x-treg-route-strict-filters",
                              b"x-treg-route-verify", b"x-treg-route-confirm",
                              b"x-treg-route-decisive", b"x-treg-route-quality",
                              b"x-treg-route-upgrade", b"x-treg-route-complete", b"host"})
_CALLER_FAULT = frozenset({400, 401, 403, 404, 405, 409, 422})
_CANDIDATE_LOCAL_FAILURES = frozenset({"tool_access_denied", "policy_denied", "capability_pinned",
                                       "org_endpoint_quota_exceeded"})
_QUOTA_REFUSALS = frozenset({"org_endpoint_quota_exceeded"})
_GLOBAL_REFUSALS = frozenset({"insufficient_balance", "free_credit_excludes_video",
                              "tag_spend_cap_reached", "platform_daily_cap_reached",
                              "member_spend_cap_reached", "daily_cap_reached"})
# treg's own transient refusals for the whole team (or tag), answered before any hold: every
# candidate on treg's key meets the same busy money queue or the same unreadable spend ceiling.
_TEAM_BUSY = frozenset({"money_admission_timeout", "platform_cap_unavailable", "tag_budget_unavailable",
                        "member_spend_cap_unavailable"})


MAX_TEAM_BUSY_TRIES = 2  # candidates asked while the team's own money queue or budget read is busy
MAX_WEAK_FALLBACKS = 2   # extra providers asked after a thin-but-real answer (see min_results)
CHEAP_RETRY_MICRO = 10_000  # ≤ 1¢: a per_call provider cheap enough to be asked after another's 4xx
ROUTED_ASYNC_WAIT_SECONDS = 60


def _over_ceiling(held: int | None, remaining: int) -> str:
    """The skip note for a child its own reserve refused: the hold that did not fit (the adapted
    request's price with margin) and the ceiling left, so a caller can size the ceiling."""
    if not isinstance(held, int):
        return "would exceed max cost"
    return f"would exceed max cost: would hold ${ledger.usd(held):g}, ${ledger.usd(max(0, remaining)):g} left"


def _free_on_failure(cand: Candidate) -> bool:
    """A candidate worth asking after another provider's 4xx: it bills nothing for a rejected request
    (per_success pricing, a free endpoint, the org's own key) or so little (≤ 1¢ per call) that a
    repeated mistake costs less than failing the caller — scrapers answer 400 for their own outages."""
    if cand.tier == "credential" or not cand.price_micro:
        return True
    if (cand.endpoint.get("cost") or {}).get("type") == "per_success":
        return True
    return cand.price_micro <= CHEAP_RETRY_MICRO


def _filled(contract, core: dict) -> int:
    """How many of the contract's output fields an answer carries."""
    return sum(1 for k in contract.output if core.get(k) not in (None, "", [], {}))


def _thin(contract, core: dict) -> str | None:
    """`X-Treg-Route-Complete`: why an answer is too thin to end the route, else None. The
    contract's `complete` fields when it names them, else more than half of its output."""
    if contract.complete:
        missing = [k for k in contract.complete if core.get(k) in (None, "", [], {})]
        return f"no {', '.join(missing)}" if missing else None
    n = _filled(contract, core)
    return f"{n} of {len(contract.output)} output fields" if 2 * n < len(contract.output) else None


def _can_answer_fully(contract, adapter) -> bool:
    """`X-Treg-Route-Complete` ranking: an adapter that cannot map what a complete answer carries
    can only ever give a thin one, so it is asked after every adapter that can do better."""
    if contract.complete:
        return all(k in adapter.out_map for k in contract.complete)
    return 2 * len(set(adapter.out_map) & set(contract.output)) >= len(contract.output)


def _subject(key: str, value) -> str:
    """A comparable form of an identity value: the profile handle for a LinkedIn URL, else the text
    without a URL's scheme, `www.` and trailing slash (the identity's URL is normalised, a provider's echo is not)."""
    if key == "linkedin_url" or key.endswith("_handle"):
        return (P.linkedin_handle(value) or str(value)).strip("/").lower()
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", str(value).strip().lower()).rstrip("/")


def _other_subject(contract, identity: dict, core: dict) -> str | None:
    """The identifier an answer reports for its subject when it differs from the one asked (an
    identity field the contract also returns), else None. A renamed profile reports its new handle,
    a wrong match someone else's: `_complete_check` tells them apart by a second provider."""
    for k in contract.output:
        if k in contract.identity_types and identity.get(k) not in (None, "") and core.get(k) not in (None, ""):
            if _subject(k, core[k]) != _subject(k, identity[k]):
                return _subject(k, core[k])
    return None


_DOMAIN_IDENTITY_KEYS = ("domain", "company_domain", "website")


def _email_domain_match(identity: dict, output: dict | None) -> bool | None:
    """True/False when the caller named a domain and the hit carries an email; else None.

    Same comparison call-events uses for `answer_flags.domain_match`. Disclose on `_treg` and
    `X-Treg-Domain-Match`; never alter the child's answer. A LinkedIn-only child often returns a
    former employer when the caller also sent a domain.
    """
    if not output:
        return None
    email = output.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    asked = next((identity[k] for k in _DOMAIN_IDENTITY_KEYS if identity.get(k) not in (None, "")), None)
    if asked in (None, ""):
        return None
    asked_host = P.host(asked)
    got_host = P.host(email.rsplit("@", 1)[1])
    if not asked_host or not got_host:
        return None
    return got_host == asked_host


def _miss_status(endpoint: dict) -> int | None:
    """See `routing.contracts.miss_status` — kept as the router's name for it (tests pin it)."""
    return miss_status(endpoint)


def _declared_miss(endpoint: dict, status: int, raw: bytes) -> bool:
    """See `routing.contracts.declared_miss`: the router and the arena read the `miss:` block
    through the same function, so a prospeo 400 INVALID_DATAPOINTS is an error in both."""
    if not declared_miss(endpoint, status, raw):
        return False
    if (endpoint.get("miss") or {}).get("when"):
        log.debug("declared miss by predicate on %s", endpoint.get("id"))
    return True


@dataclass(frozen=True)
class ChildOutcome:
    """How the router reads one child's answer, before any waterfall option is applied.

    `kind` is `declared_miss`, `not_found`, `rejected` (a 4xx the caller caused), `error`,
    `adapter_error` (the adapter threw on the body), `miss` or `hit`. A `miss` names its reasons
    in `why`; a `hit` carries the parsed body, the contract's core output and its row count."""

    kind: str
    why: tuple[str, ...] = ()
    detail: str = ""
    doc: object = None
    core: dict = field(default_factory=dict)
    rows: int = 0


def child_outcome(endpoint: dict, contract, adapter, status: int, raw: bytes, *,
                  tier: str, identity: dict | None = None) -> ChildOutcome:
    """Classify one routed child's answer. Pure: reads only this checkout's catalog."""
    if _declared_miss(endpoint, status, raw):
        return ChildOutcome("declared_miss")
    if declared_not_found(endpoint, status, raw):
        return ChildOutcome("not_found")
    capacity_signal = classify_capacity(endpoint["provider"], status, body=raw)
    if tier == "platform" and is_exhausting(capacity_signal):
        # treg's own account is out of money or out of its period allowance: nothing about the
        # question, so the attempt is skipped like a rate-limited one, never the route's error.
        return ChildOutcome("account_exhausted", detail=capacity_signal.kind)
    temporary_capacity = capacity_signal is not None and capacity_signal.kind in ("burst", "unknown")
    platform_auth_failure = tier == "platform" and status in (401, 403)
    # 405 and 410 Gone say the PROVIDER cannot serve this route (a discontinued endpoint answers
    # 410 to every request), whatever the caller sent: fall over as after a 5xx, paid providers too.
    if (400 <= status < 500 and status not in (402, 405, 408, 410, 429)
            and not platform_auth_failure and not temporary_capacity):
        return ChildOutcome("rejected")
    if not 200 <= status < 300:
        return ChildOutcome("error")
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    try:
        core = adapter.from_upstream(doc) if doc is not None else {}
    except Exception as exc:  # noqa: BLE001 — adapter threw on provider response
        return ChildOutcome("adapter_error", detail=f"adapter.from_upstream failed: {exc}"[:120])
    # A miss is what the adapter's predicate says - OR a 2xx whose body does not carry the
    # contract's required core (a null `result` under a 200, an error task inside a 20000
    # envelope): the caller asked for the field and did not get it.
    empty_core = any(core.get(k) in (None, "", [], {}) for k in contract.required_output)
    try:
        is_miss = adapter.is_miss(doc) if doc is not None else True
    except Exception as exc:  # noqa: BLE001 — adapter threw on provider response
        return ChildOutcome("adapter_error", detail=f"adapter.is_miss failed: {exc}"[:120])
    # The provider's own `expect` success rule (a DataForSEO task status other than 20000 under
    # a 20000 envelope) says this request failed: not an answer, whatever the remains read as.
    upstream_failed = catalog_results.fails_expect(endpoint["id"], raw)
    provider_error = False
    if is_v2(endpoint):
        judged = catalog_outcome.judge(endpoint, status, doc,
                                       answer=plugins.Answer(status=status, headers={}, raw=raw, body=doc))
        provider_error = judged.cause == "error"
        is_miss = is_miss or judged.cause == "no_content"
    off_question = False
    rule = getattr(contract, "question_miss", "")
    if rule and identity and doc is not None and not (is_miss or empty_core):
        try:
            off_question = bool(P.evaluate(rule, {**identity, **core}))
        except Exception as exc:  # noqa: BLE001 — a rule that cannot read this answer judges nothing
            log.debug("question_miss failed on %s: %s", endpoint.get("id"), exc)
    if doc is None or is_miss or empty_core or upstream_failed or provider_error or off_question:
        why = tuple(name for name, on in (
            ("non_json", doc is None), ("adapter_miss", is_miss), ("empty_core", empty_core),
            ("expect_failed", bool(upstream_failed)), ("provider_error", provider_error),
            ("question_miss", off_question)) if on)
        return ChildOutcome(
            "miss", why=why, doc=doc,
            detail=f"provider reported failure: {upstream_failed}" if upstream_failed else "")
    rows = max((len(v) for v in core.values() if isinstance(v, list)), default=1)
    return ChildOutcome("hit", doc=doc, core=core, rows=rows)


CURSOR_MAX = 512  # chars of a provider cursor `output.next_cursor` carries; longer stays in `raw`
DEFAULT_MAX_COST_MICRO = 1_000_000  # $1.00 per routed call unless the caller says otherwise — a runaway guard, not a budget


@dataclass
class RouteOptions:
    waterfall: bool = True
    max_cost_micro: int | None = DEFAULT_MAX_COST_MICRO
    prefer: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    min_results: int = 1     # a hit with fewer rows than this is WEAK: keep looking, keep the best
    merge: bool = False      # union the rows of every attempt that returned some (list answers only)
    strict_filters: bool = False  # drop any candidate that cannot express a filter the caller sent
    verify: bool = False     # after a hit, run the contract's check (`X-Treg-Route-Verify`)
    confirm: bool = False    # a hit needs a second provider's same answer (`X-Treg-Route-Confirm`)
    decisive: bool = False   # a hit needs a deciding verdict word (`X-Treg-Route-Decisive`)
    quality: bool = False    # ask providers in order of measured answer quality (`X-Treg-Route-Quality`)
    upgrade: str | None = None  # re-ask by this identity key when the hit carries it (`X-Treg-Route-Upgrade`)
    complete: bool = False   # only a full answer about the subject asked counts (`X-Treg-Route-Complete`, see `_complete_check`)

    @classmethod
    def from_headers(cls, get, default_max_cost_micro: int | None = None) -> "RouteOptions":
        def _list(v):
            return [p.strip() for p in (v or "").split(",") if p.strip()]
        mc = get(MAX_COST_HEADER)
        try:
            max_cost = int(round(float(mc) * 1_000_000)) if mc else (
                default_max_cost_micro if default_max_cost_micro is not None else DEFAULT_MAX_COST_MICRO)
        except (ValueError, OverflowError) as exc:
            raise ResolutionFailed("catalog_parameter_invalid", status_code=400, rule="type",
                                   detail=f"{MAX_COST_HEADER} must be a USD number, got {mc!r}") from exc
        wf = str(get(WATERFALL_HEADER) or "").strip().lower()
        try:
            mr = max(1, int(get(MIN_RESULTS_HEADER) or 1))
        except ValueError as exc:
            raise ResolutionFailed("catalog_parameter_invalid", status_code=400, rule="type",
                                   detail=f"{MIN_RESULTS_HEADER} must be a whole number, got {get(MIN_RESULTS_HEADER)!r}") from exc
        mg = str(get(MERGE_HEADER) or "").strip().lower()
        sf = str(get(STRICT_FILTERS_HEADER) or "").strip().lower()
        vf = str(get(VERIFY_HEADER) or "").strip().lower()
        cf = str(get(CONFIRM_HEADER) or "").strip().lower()
        dc = str(get(DECISIVE_HEADER) or "").strip().lower()
        ql = str(get(QUALITY_HEADER) or "").strip().lower()
        up = str(get(UPGRADE_HEADER) or "").strip()
        cp = str(get(COMPLETE_HEADER) or "").strip().lower()
        return cls(waterfall=wf not in ("0", "false", "no", "off"),
                   max_cost_micro=max_cost, prefer=_list(get(PREFER_HEADER)), exclude=_list(get(EXCLUDE_HEADER)),
                   min_results=mr, merge=mg in ("1", "true", "yes", "on"), strict_filters=sf in ("1", "true", "yes", "on"),
                   verify=vf in ("1", "true", "yes", "on"), confirm=cf in ("1", "true", "yes", "on"),
                   decisive=dc in ("1", "true", "yes", "on"), quality=ql in ("1", "true", "yes", "on"),
                   upgrade=up or None, complete=cp in ("1", "true", "yes", "on"))


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
    outcome: str            # hit | weak | miss | not_found | error | rejected | skipped
    status: int | None
    charged_micro: int
    detail: str = ""
    ignored: tuple[str, ...] = ()  # filters the caller sent that this provider's adapter has no place for

    def view(self) -> dict:
        return {"endpoint_id": self.endpoint_id, "provider": self.provider, "outcome": self.outcome,
                "status": self.status, "charged_micro": self.charged_micro, **({"detail": self.detail} if self.detail else {}),
                **({"ignored_filters": list(self.ignored)} if self.ignored else {})}


def _above(value: object, cap: int) -> bool:
    """A filter value above a provider's cap: a number, or a string of ASCII digits."""
    if isinstance(value, str) and value.isascii() and value.isdigit():
        digits = value.lstrip("0")
        return len(digits) > len(str(cap)) or int(digits or "0") > cap
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > cap


def _require_ranges(ep: dict, filters: dict, given: dict) -> None:
    """A routed call's own filter that declares a range is, when the caller sends it, a whole number
    in that range read as a count is read (a JSON integer, or ASCII digits), or the routed call is
    refused before anything is planned or held."""
    for name, spec in (filters or {}).items():
        if "min" not in (spec or {}):
            continue
        value = (given or {}).get(name)
        if value not in (None, "") and not call_cost._whole(value, spec.get("max"), spec["min"]):
            bound = f"from {spec['min']} to {spec['max']}" if "max" in spec else f"of at least {spec['min']}"
            raise call_cost._invalid(ep, f"body.{name}", "range", f"{ep['id']} needs {name}: a whole number {bound}")


def _require_counts(ep: dict, candidates: list, filters: dict, given: dict) -> None:
    """A routed call's own filter that a v2 candidate maps into its `reserve.rows` count is a whole
    number from 1 to that row's max when the caller sends it, or the routed call is refused before
    any candidate is held. Neither the child nor the value is named."""
    for e, ad, _v in candidates:
        rule = (e.get("reserve") or {}).get("rows") or {}
        if not is_v2(e) or not rule.get("param"):
            continue
        param = rule["param"]
        target = "queryParams." + param.removeprefix("query.") if param.startswith("query.") else param
        names = {k for k, t in ad.in_map.items() if t == target} | {
            n for t, expr in (ad.in_expr or {}).items() if t == target for n in re.findall(r"[A-Za-z_]\w*", expr)}
        for name in sorted(names & set(filters or {})):
            value = (given or {}).get(name)
            if value not in (None, "") and not call_cost._whole(value, int(rule["max"])):
                raise call_cost._invalid(ep, name, "range", f"{ep['id']} needs {name}: a whole number from 1 to {rule['max']}")


def _empty_output(contract) -> dict:
    """The contract's output shape for a miss / pending / no-core answer.

    Required list fields are `[]`, never `null`: the catalog types `results` / `people` / … as lists,
    and callers (and the miss rule `results == []`) treat null as a broken response rather than an
    empty one. The list's companion `count` (int) is `0`; every other scalar — including other ints
    such as `followers` — stays `null` so an all-miss is not a fabricated zero measurement.
    """
    out = {}
    for k, spec in contract.output.items():
        t = (spec or {}).get("type")
        if t == "list":
            out[k] = []
        elif t == "int" and k == "count":
            out[k] = 0
        else:
            out[k] = None
    return out


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


def _adapter_uses_limit(adapter) -> bool:
    """True when the adapter forwards `limit` (directly or through an expression). A child that
    never sees limit answers at its own page size; that is disclosed after the fact, not ranked."""
    used = set(adapter.in_map) | {
        n for e in (adapter.in_expr or {}).values() for n in re.findall(r"[A-Za-z_]\w*", e)
    }
    return "limit" in used


def _long_cursor(output: dict | None, adapter, merged: bool) -> tuple[dict | None, dict | None]:
    """A `next_cursor` longer than CURSOR_MAX leaves `output` and is named on `_treg` instead.
    A routed endpoint takes no cursor, so the token only serves a direct call to the provider,
    and that call reads it from `raw`, where it stays whole (icypeas: an 18 KB token, twice)."""
    cursor = (output or {}).get("next_cursor")
    if not isinstance(cursor, str) or len(cursor) <= CURSOR_MAX:
        return output, None
    path = (adapter.out_map.get("next_cursor") if adapter is not None and not merged else None)
    return {**output, "next_cursor": None}, {"chars": len(cursor), **({"in_raw": path} if path else {})}


def _limit_exceeded_note(*, asked: int, returned: int, served_by: str, merged: bool, adapter) -> dict:
    """Honest disclosure when a routed answer carries more rows than the caller asked for."""
    if merged:
        why = "merged answers exceeded the asked limit"
    elif adapter is not None and not _adapter_uses_limit(adapter):
        why = f"{served_by} does not accept limit"
    else:
        why = f"{served_by} returned more rows than asked"
    return {"asked": asked, "returned": returned, "why": why}


def _merge_rows(field: str, answers: list[tuple], limit: int | None = None):
    """Union in ATTEMPT order — the ranking of the provider that answered first leads, later
    providers append what they add. The caller already paid for every one of these rows
    (`spent` sums every attempt), so discarding them is pure waste. `limit` is accepted for
    call-site clarity but never applied: excess is disclosed on `_treg.limit_exceeded` instead."""
    del limit  # kept in the signature so call sites stay readable; never trim paid rows
    seen, out = set(), []
    for _cand, _doc, core, _raw in answers:
        for r in (core.get(field) or []):
            k = _row_key(r)
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            out.append(r)
    return out


async def build_plan(ep: dict, identity_given: dict, caller, options: RouteOptions, *,
                     probe: bool = False) -> Plan:
    """Candidates for this org: adapters that accept the identity, own keys first, exhausted
    providers dropped, ranked by expected cost per hit. Reads only; nothing reserved. `probe`: this
    plan will be run, so it may take a failing child's probe turn (`route_health.probe`); a
    preview never does."""
    cat = catalog_store.load()
    contract = cat.contracts.get(ep["capability"])
    if contract is None:
        raise ResolutionFailed("target_not_found", status_code=404, detail=f"{ep['id']} has no contract")
    identity, variant = canonical_identity(contract, identity_given)
    sample_percentages = get_settings().route_provider_sample_percentages
    if variant is None:
        raise ResolutionFailed("route_no_candidate", status_code=422, detail={
            "error": "identity_incomplete", "endpoint_id": ep["id"],
            "message": f"{ep['id']} needs exactly one identity variant in the JSON body: "
                       + " | ".join("{" + ", ".join(v) + "}" for v in contract.identity),
            "variants": [list(v) for v in contract.identity]})
    raw, dropped = candidates_for(contract, cat.for_capability(ep["capability"]), cat.adapters, identity)
    # A provider that caps a filter (`max: {limit: 8}`) is skipped for a request above the cap
    # rather than answering less than was asked; the drop says so.
    capped = []
    for e, ad, v in raw:
        over = [f"{k} <= {m}" for k, m in (getattr(ad, "filter_max", None) or {}).items()
                if _above(identity.get(k), m)]
        if over:
            dropped.append({"endpoint_id": e["id"], "why": "needs " + ", ".join(over)})
        else:
            capped.append((e, ad, v))
    raw = capped
    _require_counts(ep, raw, contract.filters, identity_given)
    scoped = []
    for e, ad, v in raw:
        if missing := unscoped(ad, contract, identity):
            dropped.append({"endpoint_id": e["id"], "why": f"cannot scope by {', '.join(missing)}; "
                            "it would answer the same for any value"})
        else:
            scoped.append((e, ad, v))
    raw = scoped
    ids = [e["id"] for e, _, _ in raw]
    stats = await _observed_stats(ids)
    own: set[str] = set()
    own_tools: set[str] = set()
    tool_hosts: set[str] = set()  # hosts the team registered a tool for (a row on another host is not covered)
    if ids:
        from sqlalchemy import select as _select
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
                if hosts or any(e.get("host") for e, _, _ in raw):
                    tool_hosts = set((await db.execute(
                        _select(Tool.host).where(Tool.org_id == caller.org_id))).scalars().all())
                    own_tools = {hosts[h] for h in tool_hosts if h in hosts}
            for service in services:
                # no skip for a provider with a team tool: a row on another of its hosts is not
                # covered by that tool, so the team's own key must still be found for it
                registered = oauth_providers.get(service)
                if registered is not None and registered.auth_kind == "public":
                    continue  # nothing to connect: a same-named secret is not its credential
                if caller.org_id is not None and await _marketplace_secret(service, caller.org_id, db) is not None:
                    own.add(service)
    # A `false` flag asks for nothing (`contents: false` is the default answer), so it is not sent.
    given = {k for k, v in (identity_given or {}).items() if v not in (None, "") and v is not False}
    cands: list[Candidate] = []
    for e, ad, v in raw:
        st = stats.get(e["id"]) or {}
        anonymous = _anonymous_offer(e, caller.org) is not None
        tier = (
            # a row declaring its own host (Lever EU) is the team's tool only on that host
            "tool" if (e.get("host") in tool_hosts if e.get("host") else e["provider"] in own_tools) else
            "credential" if e["provider"] in own else
            "anonymous" if anonymous else
            "platform"
        )
        cv = cat.cost_view(e.get("cost"), e["provider"])
        direct_exhausted = tier == "platform" and keyslots.all_exhausted(
            e["provider"], e["id"], metered=spends_balance((e.get("cost") or {}).get("type")))
        overflow_route = None
        if (direct_exhausted and get_settings().overflow_mode == "on"
                and not getattr(caller.org, "platform_overflow_disabled", False)):
            overflow_route = next(iter(overflow_routes_view.for_endpoint(e["id"])), None)
        price = 0 if tier != "platform" else cost_at(cv, identity, ad)
        miss = 0 if tier != "platform" else miss_charge_micro(cv, identity, ad, price)
        if overflow_route is not None:
            # The aggregator settles on its own envelope or its reserve, not on the direct row's
            # count: one priced per call bills an empty answer too, one priced per result
            # reports no rows and charges nothing for it.
            price = overflow_route.agg_price_micro
            miss = price if getattr(overflow_route, "agg_unit", "call") == "call" else 0
        c = Candidate(endpoint=e, adapter=ad, variant=v, tier=tier, price_micro=price, hit_rate=st.get("hit_rate"),
                      ok_rate=st.get("ok_rate"), p50_ms=st.get("p50_ms"), last_ok_days=st.get("last_ok_days"),
                      p95_ms=st.get("p95_ms"),
                      exhausted=direct_exhausted and overflow_route is None,
                      note=(f"direct account exhausted; overflow via {overflow_route.aggregator}"
                            if overflow_route is not None else ""),
                      ignored=ignored_filters(ad, contract, identity, given), miss_micro=miss)
        if catalog_store.paused(e):
            dropped.append({"endpoint_id": e["id"], "why": f"{e['provider']} is paused on this deployment"})
            continue
        if (tier == "platform" and e.get("platform_auth") == "anonymous"
                and getattr(oauth_providers.get(e["provider"]), "auth_kind", "") == "public"):
            # a keyless row the anonymous tier did not take: say why, never "no key" for a keyless API
            demo = caller.org is not None and (demo_sandbox.is_sandbox(caller.org) or caller.org.public_demo)
            dropped.append({"endpoint_id": e["id"], "why": f"{e['provider']} {_anonymous_refusal(e, caller.org)}",
                            **({"policy": True} if demo else {})})  # treg's demo policy, not capacity
            continue
        if tier == "platform" and not cat.platform_eligible(e):
            dropped.append({"endpoint_id": e["id"], "why": "not platform-eligible and no own key"})
            continue
        if tier == "platform" and not get_settings().platform_key_for(e["provider"]):
            # priceable, but this deployment holds no key for the provider: the child would answer
            # "no credential" (a 404 the router must not treat as the caller's fault).
            dropped.append({"endpoint_id": e["id"], "why": f"no {e['provider']} key on this deployment and no own key"})
            continue
        if tier == "platform" and not sampled_provider_allowed(
                e["provider"], sample_percentages, org_id=caller.org_id,
                capability=ep["capability"], identity=flags_off(contract, identity)):
            dropped.append({"endpoint_id": e["id"], "why": "not selected for this routed request"})
            continue
        if c.exhausted:
            dropped.append({"endpoint_id": e["id"], "why": "treg's account for this provider is exhausted right now"})
        cands.append(c)
    if options.strict_filters:
        # The caller would rather get nothing than a LOOSER answer: a candidate that cannot express
        # a filter it sent is dropped, not ranked last. The drop names the filters and what the
        # adapter DOES take, so an agent can re-ask with an identity a geo-aware child accepts.
        kept = []
        for c in cands:
            if c.ignored:
                wants = " | ".join("{" + ", ".join(a) + "}" for a in c.adapter.accepts)
                dropped.append({"endpoint_id": c.endpoint["id"], "strict": True,
                                "why": f"cannot express {', '.join(c.ignored)} (X-Treg-Route-Strict-Filters); it takes {wants}"})
            else:
                kept.append(c)
        cands = kept
    health = await _apply_health(cands, dropped, probe=probe, exclude=options.exclude,
                                 max_cost_micro=options.max_cost_micro)
    decision = None
    if route_rules_app.enabled():
        input_type, meta, _ = call_events.segment(contract, identity_given)
        decision = route_rules_app.decide(contract.capability, {**meta, "input_type": input_type})
    rule = decision.rule if decision else None
    route_rule: dict[str, str] = {}
    if decision is not None:
        route_rule["route_rules_version"] = str(decision.version)
    if rule is not None:
        route_rule["route_rule"] = rule.id

    def ordered(prefer):
        out = rank(cands, prefer=prefer, exclude=options.exclude,
                   given=given, derive=contract.derive, one_subject=contract.one_subject,
                   caller_prefer=bool(options.prefer))
        if options.complete:  # stable: price order within each group
            out = [c for c in out if _can_answer_fully(contract, c.adapter)] + \
                  [c for c in out if not _can_answer_fully(contract, c.adapter)]
        return out

    orders: dict[str, list[str]] = {}
    if options.prefer:
        ranked = ordered(options.prefer)
    else:
        default = default_prefer(contract, identity)
        ranked = ordered(default)
        orders["default"] = _endpoints(ranked)
        if rule is not None:
            ruled = ordered([*rule.order, *(p for p in default if p not in rule.order)])
            orders["rule"] = _endpoints(ruled)
            ranked = ruled
    return Plan(contract=contract, identity=identity, variant=variant, candidates=ranked, dropped=dropped,
                route_rule=route_rule, orders=orders, health=health)


_GUARDED_TIERS = ("platform", "anonymous")   # a team's own key is never skipped or reordered for health


async def _apply_health(cands: list[Candidate], dropped: list[dict], *, probe: bool,
                        exclude: list[str] = (), max_cost_micro: int | None = None) -> dict[str, str]:
    """Read the short-window health of every treg-paid child and act on it (`route_health`).
    `on`: a failing child is dropped unless it takes this call's probe turn or nothing else would
    be left, a slow one ranks after the healthy ones of its tier. `observe`: only reported.
    Returns the verdicts that are not `ok`, by endpoint id."""
    mode = route_health.mode()
    excluded = {p.lower() for p in exclude}
    askable = [c for c in cands if c.endpoint["provider"].lower() not in excluded and not c.exhausted]
    guarded = [c for c in askable if c.tier in _GUARDED_TIERS]
    # "Something else to ask" counts only what the cost ceiling lets the walk start.
    affordable = [c for c in askable if max_cost_micro is None or (c.price_micro or 0) <= max_cost_micro]
    if mode == "off" or not guarded:
        return {}
    tallies = await route_health.read([c.endpoint["id"] for c in guarded])
    limits = route_health.thresholds()
    verdicts: dict[str, str] = {}
    failing: list[tuple[Candidate, health_rules.Tally]] = []
    for c in guarded:
        tally = tallies.get(c.endpoint["id"], health_rules.Tally())
        v = health_rules.verdict(tally, limits)
        if v == health_rules.OK:
            continue
        verdicts[c.endpoint["id"]] = v
        if mode != "on":
            continue
        if v == health_rules.FAILING:
            failing.append((c, tally))
        else:
            c.health = v
    if not failing:
        return verdicts
    failing_ids = {id(c) for c, _ in failing}
    kept = sum(1 for c in affordable if id(c) not in failing_ids)
    rest = []
    for c, tally in failing:
        if probe and await route_health.probe(c.endpoint["id"]):
            verdicts[c.endpoint["id"]] = "probe"   # asked where it ranks, to see whether it is back
            kept += any(c is a for a in affordable)
        else:
            rest.append((c, tally))
    for c, tally in rest:
        if kept == 0 and any(c is a for a in affordable):
            c.health = health_rules.FAILING        # nothing else to ask: kept, last
            continue
        cands.remove(c)
        dropped.append({"endpoint_id": c.endpoint["id"], "health": health_rules.FAILING,
                        "why": f"failing right now: {tally.failed} of its last {tally.calls} routed calls failed"})
    return verdicts


class _ChildTimedOut(Exception):
    """A routed child ran past its cancellation deadline (`_child_timeout_s`)."""


async def _bounded(deadline: float | None, awaitable):
    """Await within the child's deadline (loop time), in this task: contextvars and the child's
    own cancellation compensation run as on any cancelled call."""
    if deadline is None:
        return await awaitable
    try:
        async with asyncio.timeout_at(deadline) as bound:
            return await awaitable
    except TimeoutError:
        if bound.expired():
            raise _ChildTimedOut() from None
        raise


def _child_bound_s(cand: Candidate) -> float | None:
    """The child's time bound: what its health counts as slow (past half of it) and, for a
    synchronous child, when it is cancelled (`_child_timeout_s`). None when the bound is off."""
    s = get_settings()
    return health_rules.child_timeout_s(s.route_child_timeout_s, s.call_timeout_s, cand.p95_ms)


def _child_timeout_s(cand: Candidate) -> float | None:
    """When the child is cancelled; None for an async child, which still counts slow by its bound.
    Once the provider accepts a submission its task row and hold belong to that task (202 pending,
    the worker settles), so the child must never be cancelled after it."""
    return None if cand.endpoint.get("async") else _child_bound_s(cand)


def _refund_errors(errored: list, pending: list, spent: int) -> int:
    """An incomplete miss bills the misses, never an answer that failed on the way: each billed
    `error` attempt's deferred hold is released, and what is left charged is returned."""
    for attempt, ref in errored:
        if attempt.charged_micro and _unbill(pending, ref):
            spent -= attempt.charged_micro
            attempt.charged_micro = 0
    return spent


def _errored(errored: list, call_ref: str, attempt: Attempt) -> Attempt:
    errored.append((attempt, call_ref))
    return attempt


def _unbill(pending: list, call_ref: str) -> bool:
    """The caller pays nothing for this child: its deferred hold, if it left one, is released with
    the others rather than settled (`close_deferred` closes each exactly once). True when it had one."""
    found = False
    for item in pending:
        if item.call_id == call_ref and item.billable:
            item.billable = False
            found = True
    return found


async def _close_quietly(response: UpstreamResponse) -> None:
    try:
        await response.close()
    except Exception:
        log.debug("closing a timed-out child response failed", exc_info=True)


def _note_health(cand: Candidate, child: CallContext, *, failed: bool, slow: bool) -> None:
    """Count a treg-paid child's answer toward its short-window health (`route_health`). A local
    refusal or an archived answer says nothing about the provider now, so neither counts."""
    if cand.tier not in _GUARDED_TIERS or not child.reached_provider or child.cached:
        return
    try:
        route_health.note(cand.endpoint["id"], failed=failed, slow=slow)
    except Exception:
        log.warning("route health note failed", exc_info=True)


def _endpoints(cands) -> list[str]:
    return [c.endpoint["id"] for c in cands]


def _child_input(parent, ep: dict, query: dict[str, str], body: dict,
                 remaining_micro: int | None = None, extra_headers: tuple = ()) -> object:
    from .types import CallInput
    has_body = ep["method"] in ("POST", "PUT", "PATCH") and body is not None
    payload = json.dumps(body).encode() if has_body else b""
    headers = [(k, v) for k, v in parent.input.raw_headers if k.lower() not in _DROP_FROM_CHILD]
    if remaining_micro is not None:
        # Replace the parent's total ceiling with the unspent amount. The child validates its
        # resolved, margin-inclusive reservation; advisory candidate prices cannot authorize spend.
        ceiling = f"{remaining_micro // 1_000_000}.{remaining_micro % 1_000_000:06d}"
        headers.append((MAX_COST_HEADER.encode(), ceiling.encode()))
    headers += list(extra_headers)
    if has_body:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    items = tuple(query.items())
    return CallInput(method=ep["method"], raw_rest=ep["id"], raw_headers=tuple(headers), query_items=items,
                     raw_query=urlencode(items), body=_Bytes(payload), caller=parent.input.caller,
                     client_ip=parent.input.client_ip, catalog_only=parent.input.catalog_only)


# A routed call parses each child's answer, so it holds the whole of it, as settlement does: an
# anonymous or own-key child would otherwise stream any size into memory.
ROUTED_BODY_MAX = 8 * 1024 * 1024


class _ChildTooLarge(Exception):
    """A child's answer passed `ROUTED_BODY_MAX`: no answer from this child, nothing billed."""


async def _read(response: UpstreamResponse) -> bytes:
    chunks, size = [], 0
    try:
        async for chunk in response.body_stream:
            size += len(chunk)
            if size > ROUTED_BODY_MAX:
                raise _ChildTooLarge(f"answer is over treg's {ROUTED_BODY_MAX >> 20} MiB routed read")
            chunks.append(chunk)
    finally:
        await response.close()
    return b"".join(chunks)


def _finished_on_submission(descriptor: dict, raw: bytes) -> bool:
    """The child already answered (`terminal_on_submission`): judged like any synchronous answer."""
    try:
        return asynctasks_rules.finished_on_submission(descriptor, json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        return False


async def _remember_pending_task(parent: CallContext, endpoint_id: str, descriptor: dict,
                                 task_id: str, kickoff: bytes) -> None:
    """Name the child's task under the parent's call id: the id a routed caller holds."""
    try:
        poll_url = asynctasks_rules.extract_submission(descriptor, json.loads(kickoff)).poll_url
    except ValueError:
        poll_url = None
    try:
        await async_task_app.remember_routed_task(
            parent.call_ref, parent.input.caller.org_id, endpoint_id, descriptor,
            task_id, poll_url, parent.meta.tags)
    except Exception:
        log.warning("could not record the pending task of routed call %s", parent.call_ref,
                    exc_info=True)


async def _async_cost(parent: CallContext, child_ref: str, fallback: int = 0) -> int:
    """Read the original async task's terminal money truth; BYOK has no task row and costs zero."""
    org_id = parent.input.caller.org_id
    if org_id is None:
        return fallback
    views = await async_task_app.views_for(
        org_id, [child_ref], pinned_tags=getattr(parent.meta, "tags", None))
    view = views.get(child_ref) or {}
    settled = view.get("settled_micro")
    return int(settled) if settled is not None else fallback


def _header(response: UpstreamResponse, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for k, v in response.raw_headers:
        if k.lower() == wanted:
            return v.decode("latin-1")
    return None


async def run_routed(parent: CallContext, ep: dict, body_bytes: bytes, get_header, upstream_client: httpx.AsyncClient,
                     execute_child, *, audit_client: str = "") -> tuple[UpstreamResponse, int]:
    """Execute a routed call under `parent`. Returns (the assembled response, total charged).

    A child that charges nothing releases its hold at once, before the next child reserves. Every
    child that bills leaves its hold OPEN in `pending` (CallContext.deferred_settles) and this call
    closes all of them once, at the end: settled when the routed call answers (a hit, or a 200
    miss), released when it fails. A routed call that fails charges the caller nothing, even when
    a provider on the way answered and billed treg (owner decision 2026-09-21)."""
    pending: list = []
    try:
        response, spent = await _run_routed(parent, ep, body_bytes, get_header, upstream_client,
                                            execute_child, pending, audit_client=audit_client)
    except CallFailure as exc:
        released = sum(int(a.get("charged_micro") or 0) for a in (exc.detail.get("tried") or [])
                       if isinstance(a, dict)) if isinstance(exc.detail, dict) else 0
        await close_deferred(pending, charge=False, why=f"routed_{exc.kind}")
        if isinstance(exc.detail, dict):
            exc.detail["charged_micro"] = 0
            if released:
                exc.detail["released_micro"] = released
            for a in exc.detail.get("tried") or []:
                if isinstance(a, dict):
                    a["charged_micro"] = 0
        raise
    except BaseException:
        await close_deferred(pending, charge=False, why="routed_aborted")
        raise
    await close_deferred(pending, charge=True)
    return response, spent


def _no_candidate_refusal(ep: dict, plan, options: RouteOptions) -> ResolutionFailed:
    """The refusal for a routed call whose plan kept no candidate, with who it is on."""
    # 503 only when capacity or keys took a candidate away; a strict-filter drop is the
    # caller's own choice, and a provider that cannot send a key the caller gave never will,
    # so both answer 422 (nothing was charged either way)
    capacity_drop = any(not d.get("strict") and not d.get("policy")
                        and not str(d.get("why", "")).startswith(("needs ", "cannot scope by "))
                        for d in plan.dropped)
    # only treg's demo policy took every candidate: any strict, scope or filter drop beside it
    # is the caller's to fix, so it keeps its own message
    policy_only = bool(plan.dropped) and all(d.get("policy") for d in plan.dropped)
    strict_drop = [d for d in plan.dropped if d.get("strict")]
    scope_drop = [d for d in plan.dropped if str(d.get("why", "")).startswith("cannot scope by ")]
    return ResolutionFailed("route_no_candidate", status_code=503 if capacity_drop else 422,
                           log_kind="admission_denied" if policy_only else None, detail={
        "error": "no_route_candidate", "endpoint_id": ep["id"], "identity_variant": list(plan.variant),
        "dropped": plan.dropped,
        "message": (f"no provider can serve {ep['id']} for this identity right now" if capacity_drop else
                    f"no provider for {ep['id']} is open to this team: "
                    + "; ".join(f"{d['endpoint_id']} {d['why']}" for d in plan.dropped if d.get("policy"))
                    if policy_only else
                    f"no provider for {ep['id']} can send every key you sent together"
                    + (f" among those {EXCLUDE_HEADER} leaves; check that list, or send fewer keys"
                       if options.exclude else "; send fewer keys")
                    if scope_drop and not strict_drop else
                    f"no provider can serve {ep['id']} for this identity right now" if not strict_drop else
                    f"no provider for {ep['id']} can honour every filter you sent for this identity "
                    f"(X-Treg-Route-Strict-Filters): " + "; ".join(f"{d['endpoint_id']} {d['why']}" for d in strict_drop)
                    + ". Send an identity a filter-aware provider accepts, or drop the header to accept a looser answer")})


async def _run_routed(parent: CallContext, ep: dict, body_bytes: bytes, get_header, upstream_client: httpx.AsyncClient,
                      execute_child, pending: list, *, audit_client: str = "") -> tuple[UpstreamResponse, int]:
    try:
        given = json.loads(body_bytes or b"{}")
    except ValueError as exc:
        raise ResolutionFailed("catalog_parameter_invalid", status_code=400, parameter="body", rule="json",
                               detail=f"{ep['id']} expects a JSON object body") from exc
    if not isinstance(given, dict):
        raise ResolutionFailed("catalog_parameter_invalid", status_code=400, parameter="body", rule="type",
                               detail=f"{ep['id']} expects a JSON object body")
    parent.route_summary = {"identity": given, "size": len(body_bytes or b"")}
    contract = catalog_store.load().contracts.get(ep["capability"])
    options = RouteOptions.from_headers(
        get_header, int(round(contract.default_max_cost_usd * 1_000_000)) if contract and contract.default_max_cost_usd else None)
    if (contract and options.waterfall and not get_header(MIN_RESULTS_HEADER) and not get_header(MERGE_HEADER)
            and (target := contract.fill_target(given))):
        # `fill_limit_when`: the caller's `limit` is how many rows it wants, so a shorter answer
        # goes on to the next provider and the rows add up, within MAX_WEAK_FALLBACKS and the cost cap.
        options.min_results, options.merge = target, True
    if options.verify and (contract is None or contract.check is None):
        # Refused before anything is planned or held: an agent must not believe a check ran.
        checked = sorted(f"treg.{cap}" for cap, c in catalog_store.load().contracts.items() if c.check)
        raise ResolutionFailed("catalog_parameter_invalid", status_code=422, rule="unsupported", detail={
            "error": "route_verify_unsupported", "endpoint_id": ep["id"],
            "message": f"{VERIFY_HEADER} works only on {', '.join(checked)}; {ep['id']} has no check. "
                       "Nothing was charged."})
    if options.confirm and (contract is None or not contract.confirm or not options.waterfall):
        # Refused before anything is planned or held, like the check: an agent must not believe a
        # second provider was asked. With the waterfall off there is no second provider to ask.
        confirmable = sorted(f"treg.{cap}" for cap, c in catalog_store.load().contracts.items() if c.confirm)
        raise ResolutionFailed("catalog_parameter_invalid", status_code=422, rule="unsupported", detail={
            "error": "route_confirm_unsupported", "endpoint_id": ep["id"],
            "message": (f"{CONFIRM_HEADER} needs the waterfall; drop {WATERFALL_HEADER}: 0. " if contract and contract.confirm else
                        f"{CONFIRM_HEADER} works only on {', '.join(confirmable)}; {ep['id']} has no confirm field. ")
                       + "Nothing was charged."})
    if options.decisive and (contract is None or not contract.verdict_decisive or not options.waterfall):
        # Refused before anything is planned or held, like confirm: with no deciding words, or no
        # waterfall to go on with, the caller would believe an undecided answer was the last word.
        decidable = sorted(f"treg.{cap}" for cap, c in catalog_store.load().contracts.items() if c.verdict_decisive)
        raise ResolutionFailed("catalog_parameter_invalid", status_code=422, rule="unsupported", detail={
            "error": "route_decisive_unsupported", "endpoint_id": ep["id"],
            "message": (f"{DECISIVE_HEADER} needs the waterfall; drop {WATERFALL_HEADER}: 0. " if contract and contract.verdict_decisive else
                        f"{DECISIVE_HEADER} works only on {', '.join(decidable)}; {ep['id']} has no deciding verdict words. ")
                       + "Nothing was charged."})
    if options.upgrade and (contract is None or (options.upgrade,) not in contract.identity
                            or options.upgrade not in contract.output):
        # Refused before anything is planned or held: the caller would believe a second lookup ran.
        raise ResolutionFailed("catalog_parameter_invalid", status_code=422, rule="unsupported", detail={
            "error": "route_upgrade_unsupported", "endpoint_id": ep["id"],
            "message": f"{UPGRADE_HEADER} names an identity key the answer carries and {ep['id']} accepts on its own; "
                       f"{options.upgrade!r} is not one. Nothing was charged."})
    if contract and (invalid := invalid_identity(contract, given)):
        raise ResolutionFailed("route_no_candidate", status_code=422, detail={
            "error": "invalid_identity", "endpoint_id": ep["id"], "invalid": invalid,
            "message": "; ".join(f"{i['field']} must be a {i['type']}" for i in invalid)
                       + ". Omit a field you don't have rather than sending a placeholder. Nothing was charged."})
    if contract:
        _require_ranges(ep, contract.filters, given)
    await capacity_view.load()
    if get_settings().overflow_mode != "off":
        await overflow_routes_view.load()
    plan = await build_plan(ep, given, parent.input.caller, options, probe=True)
    _summarize(parent, route_rule=plan.route_rule, **({"health": plan.health} if plan.health else {}))
    if not plan.candidates:
        raise _no_candidate_refusal(ep, plan, options)
    # On a search, rank() puts specificity ahead of price, so candidates[0] can cost more than a
    # later one. Refuse only when none of them fit the ceiling, and name the true minimum.
    if options.max_cost_micro is not None and all(
            (cand.price_micro or 0) > options.max_cost_micro for cand in plan.candidates):
        cheapest = min(plan.candidates, key=lambda cand: cand.price_micro or 0)
        raise ResolutionFailed("route_max_cost", status_code=402, detail={
            "error": "route_max_cost", "endpoint_id": ep["id"], "max_cost_micro": options.max_cost_micro,
            "cheapest_micro": cheapest.price_micro, "plan": plan.view()["plan"],
            "message": f"the cheapest candidate ({cheapest.endpoint['id']}) costs more than {MAX_COST_HEADER}"})
    tried: list[Attempt] = []
    spent = 0
    errors = 0
    too_large = 0  # attempts cut by treg's own routed-read cap, not a provider failing
    rejected_by: set[str] = set()  # providers that answered a vendor 4xx
    winner: tuple[Candidate, dict, dict, bytes] | None = None
    answers: list[tuple] = []      # every attempt that returned rows, for X-Treg-Route-Merge
    weak_hits = 0
    best: tuple[int, tuple[Candidate, dict, dict, bytes]] | None = None   # best WEAK answer seen
    other_subject: list[tuple[str, tuple]] = []   # X-Treg-Route-Complete: answers about another identifier, never returned unconfirmed
    cost_capped = False            # true when any candidate was skipped due to max-cost ceiling
    rate_waits: list[int] = []     # retry_after_s of candidates skipped by treg's own rate window
    admission_refusals: list[admission.AdmissionDenied] = []  # treg money the team may not use
    team_busy: list[CallFailure] = []  # treg's own transient team-level refusals (_TEAM_BUSY)
    unconfirmed: list[tuple[str, tuple]] = []   # X-Treg-Route-Confirm: hits waiting for a second provider
    errored: list[tuple[Attempt, str]] = []     # answered-but-failed attempts and their child call ids
    confirmed_by: str | None = None
    # X-Treg-Route-Decisive: answers whose verdict did not decide, the word that came back, and the
    # measured quality the providers left are ordered by once the first attempt has not decided.
    undecided: list[tuple] = []
    undecided_words: list[str] = []
    decided_by: str | None = None
    quality = await _route_quality(ep["capability"]) if options.decisive or options.quality else None
    ordered_on: str | None = None
    queue = list(enumerate(plan.candidates))
    # X-Treg-Route-Quality: the whole plan in order of measured answer quality for this identity,
    # the unrated behind in price order. A caller's own X-Treg-Route-Prefer order stands.
    if options.quality and not options.prefer and quality:
        try:
            queue = route_quality.by_quality(queue, lambda item: item[1].endpoint["id"], quality,
                                             route_quality.variant_slice(plan.variant))
            ordered_on = route_quality.variant_slice(plan.variant)
        except Exception:  # a bad number keeps the price order
            log.warning("route quality ordering failed", exc_info=True)
    while queue:
        # A caller's own X-Treg-Route-Prefer order stands; quality orders only the router's own choice.
        if options.decisive and not options.prefer and any(t.outcome != "skipped" for t in tried):
            on = "catch_all" if "catch_all" in undecided_words else "all"
            if on != ordered_on:
                try:
                    queue = route_quality.by_quality(queue, lambda item: item[1].endpoint["id"], quality, on)
                except Exception:  # quality only orders the fallback; a bad number keeps the order it had
                    log.warning("route quality ordering failed", exc_info=True)
                    quality = None
                ordered_on = on
        n, cand = queue.pop(0)
        if options.max_cost_micro is not None and spent + (cand.price_micro or 0) > options.max_cost_micro:
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped", None, 0, "would exceed max cost"))
            cost_capped = True
            continue
        if rejected_by and (cand.endpoint["provider"] in rejected_by or not _free_on_failure(cand)):
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped", None, 0,
                                 "provider already rejected the request" if cand.endpoint["provider"] in rejected_by
                                 else "not retried on a paid provider (> 1¢/call) after a vendor 4xx"))
            continue
        try:
            query, body = cand.adapter.to_upstream(plan.identity, cand.variant)
        except Exception as exc:  # noqa: BLE001 — adapter threw; record error and try next candidate
            errors += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", None, 0,
                                 f"adapter.to_upstream failed: {exc}"[:120]))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        # A filter the caller sent that this adapter never mentions is silently NOT applied - say so
        # on the attempt. Computed at planning time, where it also ranks the candidate down.
        ignored = cand.ignored
        remaining = max(0, options.max_cost_micro - spent) if options.max_cost_micro is not None else None
        child = CallContext(input=_child_input(parent, cand.endpoint, query, body, remaining), call_ref=f"{parent.call_ref}:r{n}",
                            meta=parent.meta, deferred_settles=pending,
                            quota_admissions=parent.quota_admissions,
                            quota_pending=parent.quota_pending)
        bound_s, cancel_s = _child_bound_s(cand), _child_timeout_s(cand)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + cancel_s if cancel_s is not None else None
        try:
            response = await _bounded(deadline, execute_child(child, upstream_client))
            _note_call(parent, plan, cand, child, tried)
        except _ChildTimedOut:
            # The child's own cancellation path released its hold (or left it to the deferred
            # close, which this marks unbillable): no answer, no charge. The next candidate runs.
            _note_call(parent, plan, cand, child, tried)
            _unbill(pending, child.call_ref)
            _note_health(cand, child, failed=True, slow=False)
            errors += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", 504, 0,
                                 f"no answer within {cancel_s:g} s; moved on"))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        except CallFailure as exc:
            _note_call(parent, plan, cand, child, tried)
            if exc.kind == "route_max_cost":
                held = (exc.detail or {}).get("estimated_cost_micro") if isinstance(exc.detail, dict) else None
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped", None, 0,
                                     _over_ceiling(held, (options.max_cost_micro or 0) - spent)))
                cost_capped = True
                continue
            if exc.kind == "org_endpoint_quota_exceeded":
                if isinstance(exc.detail, dict) and any(
                    endpoint == exc.detail.get("endpoint_id") for _, endpoint in parent.quota_pending
                ):
                    raise
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped",
                                     exc.status_code, 0, "endpoint call allowance exhausted"))
                continue
            if exc.kind == "provider_rate_limited":
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped",
                                     exc.status_code, 0, "treg's account is at its rate limit"))
                rate_waits.append(exc.retry_after_s or 1)
                continue
            if exc.kind == "admission_denied":
                # Decided for this candidate's way of paying, not for the job: a later candidate on
                # the team's own key may still serve it, so this spends no error allowance.
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped",
                                     exc.status_code, 0, "treg's money is not available to this team here"))
                admission_refusals.append(exc)
                continue
            if exc.kind in _GLOBAL_REFUSALS or exc.kind in _TEAM_BUSY:
                if unconfirmed or undecided or best is not None:
                    # An answer is already paid for: keep it, and say the next ask was refused.
                    tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped",
                                         exc.status_code, 0, f"refused by treg: {exc.kind}"))
                    break
                if exc.kind in _GLOBAL_REFUSALS:
                    raise  # the same refusal everywhere; nothing to fall back to
                # Transient and the team's own: the next candidate may find it clear, but nothing
                # here is a provider's failure, so it spends no error allowance.
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped",
                                     exc.status_code, 0, f"refused by treg: {exc.kind}"))
                team_busy.append(exc)
                if len(team_busy) >= MAX_TEAM_BUSY_TRIES:
                    break
                continue
            if exc.status_code in _CALLER_FAULT and exc.kind not in _CANDIDATE_LOCAL_FAILURES:
                raise
            if isinstance(exc, GatewayFailed):   # the provider timed out or could not be reached
                _note_health(cand, child, failed=True, slow=False)
            errors += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", exc.status_code, 0, str(exc.detail)[:120]))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        try:
            raw = await _bounded(deadline, _read(response))
        except _ChildTimedOut:
            await _close_quietly(response)
            _unbill(pending, child.call_ref)
            _note_health(cand, child, failed=True, slow=False)
            errors += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", 504, 0,
                                 f"answer not finished within {cancel_s:g} s; moved on"))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        except _ChildTooLarge as exc:
            # Too big to wrap, not the provider failing: unbilled, no health strike, and the next
            # candidate answers. The endpoint itself streams it when called directly.
            _unbill(pending, child.call_ref)
            errors += 1
            too_large += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", response.status, 0,
                                 f"{exc}; call {cand.endpoint['id']} directly, it streams"[:160]))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        except Exception as exc:  # noqa: BLE001 — failed to read child's response body
            errors += 1
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", response.status, 0,
                                 f"failed to read response: {exc}"[:120]))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        _note_health(cand, child, failed=health_rules.failed_status(response.status),
                     slow=bound_s is not None and loop.time() - started >= bound_s / 2)
        charged = int(_header(response, "X-Treg-Cost-Micro") or 0)
        descriptor = cand.endpoint.get("async")
        async_outcome = ""
        if (descriptor and 200 <= response.status < 300
                and not _finished_on_submission(descriptor, raw)):
            kickoff_raw = raw
            reserved = charged
            poll_rule = descriptor.get("poll") or {}
            poll_ep = catalog_store.load().by_id.get(poll_rule.get("endpoint"))

            # The bridge awaits every poll in this iteration; the defaults pin this iteration's values.
            async def poll_task(task_id: str, poll_number: int, *, poll_ep=poll_ep, poll_rule=poll_rule,
                                remaining=remaining, child=child):
                if poll_ep is None:
                    raise RuntimeError(f"async poll endpoint {poll_rule.get('endpoint')!r} is not catalogued")
                param = poll_rule.get("param") or {}
                name = str(param.get("name") or "id")
                poll_query = {name: task_id}
                poll_child = CallContext(
                    input=_child_input(parent, poll_ep, poll_query, {}, remaining),
                    call_ref=f"{child.call_ref}:p{poll_number}", meta=parent.meta,
                    quota_admissions=parent.quota_admissions | {
                        (parent.input.caller.org_id, poll_ep["id"])})
                poll_response = await execute_child(poll_child, upstream_client)
                return poll_response, await _read(poll_response)

            waited = await async_bridge.await_terminal(
                descriptor, kickoff_raw, poll_task, timeout_s=ROUTED_ASYNC_WAIT_SECONDS)
            # Once submission produced a task id, only a declared terminal provider status can
            # permit waterfall fallback.  A bridge error with an id is still an uncertain live
            # task, so keep the hold/worker ownership and surface it as pending.
            if waited.outcome == "pending" or (
                    waited.outcome == "error" and waited.task_id):
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "pending",
                                     202, 0, "provider is still processing", ignored=ignored))
                async_view = {
                    "task_id": waited.task_id,
                    "poll": descriptor.get("poll") or {},
                    "status": descriptor.get("status") or {},
                }
                body_out = {
                    "output": _empty_output(plan.contract),
                    "raw": json.loads(kickoff_raw),
                    "_treg": {
                        "served_by": cand.endpoint["id"], "provider": cand.endpoint["provider"],
                        "tier": cand.tier, "outcome": "pending", "tried": [t.view() for t in tried],
                        "call_ref": child.call_ref, "async": async_view,
                        "reserved_micro": reserved, "charged_micro": None,
                        **({"dropped": plan.dropped} if plan.dropped else {}),
                    },
                }
                if ":" not in parent.call_ref:
                    await _remember_pending_task(parent, cand.endpoint["id"], descriptor,
                                                 waited.task_id, kickoff_raw)
                _summarize(parent, served_by=cand.endpoint["id"], tried_count=len(tried))
                _audit_parent(parent, ep, 202, spent, audit_client)
                return _json(body_out, 202, {
                    "X-Treg-Served-By": cand.endpoint["id"],
                    "X-Treg-Providers-Tried": ",".join(t.provider for t in tried),
                    "X-Treg-Route-Outcome": "pending",
                    "X-Treg-Reserved-Micro": str(reserved),
                    "X-Treg-Async": json.dumps(async_view, separators=(",", ":")),
                    "X-Treg-Child-Call-Id": child.call_ref,
                }), spent
            if waited.response is not None:
                response = waited.response
            raw = waited.raw
            async_outcome = waited.outcome
            if waited.outcome == "error":
                errors += 1
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error",
                                     response.status if response else None, 0, waited.detail[:120]))
                break
            charged = await _async_cost(parent, child.call_ref, 0 if cand.tier != "platform" else reserved)
        spent += charged
        if async_outcome == "failure":
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "miss",
                                 response.status, charged, "asynchronous task failed", ignored=ignored))
            if options.waterfall:
                continue
            winner = (cand, {}, {}, raw)
            break
        outcome = child_outcome(cand.endpoint, plan.contract, cand.adapter, response.status, raw,
                                tier=cand.tier, identity=plan.identity)
        if outcome.kind == "declared_miss":
            # The provider's declared "asked and answered: no result" status (`miss: {status, means}`
            # on the endpoint - aviato/hunter/leadmagic/... 404 a person they have no record of),
            # optionally narrowed by a body predicate (`when`) where one status carries both a miss
            # and a fault (prospeo 400 NO_MATCH vs INVALID_DATAPOINTS). It is a MISS, not a
            # rejected request, so a waterfall whose other providers all missed ends in a 200 miss,
            # not a 502 `route_failed`, and a caller can tell "nobody has it" from "treg broke".
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "miss", response.status, charged, ignored=ignored))
            if options.waterfall:
                continue
            winner = (cand, {}, {}, raw)
            break
        if outcome.kind == "not_found":
            # The provider reports that the TARGET does not exist (the scraped site answered 404 or
            # 410). Every other provider can only find the same nothing, or answer an empty page that
            # reads as a success, so the call ends here with that answer, charging nothing.
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "not_found", response.status, charged))
            status = response.status if 400 <= response.status < 500 else 404
            _audit_parent(parent, ep, status, 0, audit_client)
            raise ResolutionFailed("route_not_found", status_code=status, detail={
                "error": "route_not_found", "endpoint_id": ep["id"], "served_by": cand.endpoint["id"],
                "tried": [t.view() for t in tried], "charged_micro": spent,
                "message": f"{cand.endpoint['id']} reports that the target does not exist",
                "provider_response": raw[:600].decode("utf-8", "replace")})
        if outcome.kind == "rejected":
            # The vendor rejected the REQUEST. Usually the caller's mistake and the same answer
            # everywhere - but a scraper's "Request failed. Please retry" is also a 400 (tikhub), so
            # the waterfall goes on to providers that are FREE ON FAILURE (per_success / free / <= 1c
            # per call, another provider, the usual error bound). A dearer paid-per-call provider is
            # never asked to bill the same mistake twice.
            # Another provider already ANSWERED this same question (a hit, a weak hit or a miss), so
            # the question is valid and this rejection is this provider's own: note it and go on,
            # like any provider error, instead of ending the whole call as the caller's fault. The
            # rows already paid for are then still returned.
            if any(a.outcome in ("hit", "weak", "miss") for a in tried):
                tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "rejected", response.status, charged,
                                     raw[:120].decode("utf-8", "replace")))
                errors += 1
                if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                    break
                continue
            tried.append(_errored(errored, child.call_ref, Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", response.status, charged, raw[:120].decode("utf-8", "replace"))))
            rejected_by.add(cand.endpoint["provider"])
            errors += 1
            if errors <= MAX_ERROR_FALLBACKS and plan.contract.idempotent and any(
                    _free_on_failure(c) and c.endpoint["provider"] not in rejected_by for c in plan.candidates[n + 1:]):
                continue
            _audit_parent(parent, ep, response.status, 0, audit_client)   # a failed routed call charges nothing
            raise ResolutionFailed("route_caller_fault", status_code=response.status, detail={
                "error": "route_caller_fault", "endpoint_id": ep["id"], "served_by": cand.endpoint["id"],
                "tried": [t.view() for t in tried], "charged_micro": spent,
                "message": f"{cand.endpoint['id']} rejected the request ({response.status}); "
                           + ("not retried elsewhere" if len(rejected_by) == 1 else "every free-on-failure provider rejected it too"),
                "provider_response": raw[:600].decode("utf-8", "replace")})
        if outcome.kind == "account_exhausted":
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "skipped", response.status, charged,
                                 f"treg's account is used up ({outcome.detail})"))
            continue
        if outcome.kind == "error":
            errors += 1
            tried.append(_errored(errored, child.call_ref, Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", response.status, charged, raw[:120].decode("utf-8", "replace"))))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        if outcome.kind == "adapter_error":
            # An adapter that throws on unexpected provider output must not crash the parent after
            # a child already answered (and was audited OK): record it as an error attempt and let
            # the waterfall go on, so the worst case is a structured `route_failed` with `tried`.
            errors += 1
            tried.append(_errored(errored, child.call_ref, Attempt(cand.endpoint["id"], cand.endpoint["provider"], "error", response.status, charged,
                                 outcome.detail)))
            if errors > MAX_ERROR_FALLBACKS or not plan.contract.idempotent:
                break
            continue
        doc, core = outcome.doc, outcome.core
        if outcome.kind == "miss":
            tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "miss", response.status, charged,
                                 outcome.detail, ignored=ignored))
            if options.waterfall:
                continue
            winner = (cand, doc if doc is not None else {}, {}, raw)
            break
        # A HIT that answers thinly is not the end of the search. `min_results` lets the caller say
        # how many rows make an answer; below it the waterfall keeps going and the best result so
        # far is kept, so a weak first provider can still win if nothing better turns up.
        n_rows = outcome.rows
        # Under a merge the distinct rows of every answer so far are what the caller gets, so
        # they, not this answer alone, decide whether to keep looking.
        have = (len(_merge_rows(lf, [*answers, (cand, doc, core, raw)]))
                if options.merge and (lf := _list_field(plan.contract)) else n_rows)
        weak = options.waterfall and have < options.min_results
        # `X-Treg-Route-Complete`: an answer carrying under half the contract's output fields is
        # weak like a short list (the fullest one wins if nothing better turns up), and an answer
        # whose subject identifier differs from the one asked waits for another provider to report
        # the same identifier: a renamed profile is confirmed, a match on someone else is not.
        thin = other = None
        if options.complete and options.waterfall and not weak:
            thin = _thin(plan.contract, core)
            if thin:
                n_rows = _filled(plan.contract, core)   # the fullest weak answer wins
            else:
                other = _other_subject(plan.contract, plan.identity, core)
        detail = (f"{have} < min_results {options.min_results}" if weak else f"thin answer: {thin}" if thin
                  else "reports another subject; needs a second provider" if other else "")
        tried.append(Attempt(cand.endpoint["id"], cand.endpoint["provider"], "weak" if weak or thin or other else "hit",
                             response.status, charged, detail, ignored=ignored))
        if other is not None:
            agreed = next((h for v, h in other_subject if v == other
                           and h[0].endpoint["provider"] != cand.endpoint["provider"]), None)
            if agreed is None:
                other_subject.append((other, (cand, doc, core, raw)))
                if len(other_subject) > MAX_WEAK_FALLBACKS:
                    break
                continue
            answers.append(agreed)
            winner = agreed
            break
        weak = weak or thin is not None
        if weak:
            if best is None or n_rows > best[0]:
                best = (n_rows, (cand, doc, core, raw))
            answers.append((cand, doc, core, raw))
            weak_hits += 1
            # Bounded like the error fallback, and for the same reason: a brief whose honest answer
            # IS one or two people (a lookup - "who runs engineering at X") never clears the bar, so
            # an unbounded rule walks the whole paid ladder on every such call.
            if weak_hits > MAX_WEAK_FALLBACKS:
                break
            continue
        if options.decisive:
            word = catalog_results.verdict(cand.endpoint["id"], response.status, raw)
            if word not in plan.contract.verdict_decisive:
                tried[-1].outcome, tried[-1].detail = "weak", f"verdict {word or 'none'} does not decide"
                undecided.append((cand, doc, core, raw))
                undecided_words.append(word or "")
                if len(undecided) > MAX_WEAK_FALLBACKS:
                    break
                continue
            decided_by = cand.endpoint["provider"]
        if options.confirm:
            # A hit counts once ANOTHER provider returns the same value (one provider's two
            # endpoints share a source). The earlier hit is the answer: it was the cheaper ask.
            value = str(core.get(plan.contract.confirm) or "").strip().lower()
            agreed = next((h for v, h in unconfirmed if v == value
                           and h[0].endpoint["provider"] != cand.endpoint["provider"]), None)
            if agreed is None:
                unconfirmed.append((value, (cand, doc, core, raw)))
                continue
            confirmed_by = cand.endpoint["provider"]
            answers.append(agreed)
            winner = agreed
            break
        answers.append((cand, doc, core, raw))
        winner = (cand, doc, core, raw)
        break
    if winner is None and unconfirmed:
        winner = unconfirmed[0][1]   # nobody agreed: the first hit, marked unconfirmed
    if winner is None and undecided:
        winner = undecided[0]        # nobody decided: the first answer, already paid for, marked undecided
    if winner is None and best is not None:
        winner = best[1]          # nobody cleared min_results — the fullest answer we paid for wins
    if winner is None:
        if tried and len(rate_waits) == len(tried):
            wait = min(rate_waits)
            _summarize(parent, tried_count=len(tried))
            _audit_parent(parent, ep, 429, 0, audit_client)
            raise GatewayFailed("provider_rate_limited", status_code=429, detail={
                "error": "provider_rate_limited", "endpoint_id": ep["id"], "retry_after_s": wait,
                "tried": [t.view() for t in tried],
                "message": f"every candidate for {ep['id']} is at treg's rate limit; nothing was "
                           f"charged, try again in {wait} s"}, retry_after_s=wait)
        if team_busy and all(t.outcome == "skipped" for t in tried):
            # Only treg's own busy refusals stopped the call: answer that, with its Retry-After,
            # rather than a route_failed that blames the providers.
            _summarize(parent, tried_count=len(tried))
            _audit_parent(parent, ep, team_busy[-1].status_code, 0, audit_client)
            raise team_busy[-1]
        if admission_refusals and all(t.outcome == "skipped" for t in tried):
            # Nothing ran: the refusal names a remedy, unlike a cost ceiling the caller may never have set.
            _summarize(parent, tried_count=len(tried))
            _audit_parent(parent, ep, 403, 0, audit_client)
            raise admission.routed_refusal(ep["id"], admission_refusals, [t.view() for t in tried])
        if tried and all(t.outcome == "skipped" for t in tried):
            raise ResolutionFailed("route_max_cost", status_code=402, detail={
                "error": "route_max_cost", "endpoint_id": ep["id"],
                "max_cost_micro": options.max_cost_micro, "charged_micro": spent,
                "tried": [t.view() for t in tried],
                "message": "no candidate fits the remaining cost ceiling; nothing was charged",
            })
        # One provider's failure does not turn the others' honest "nobody" into treg's 502: when at
        # least one provider answered a miss, the call is a miss, marked `incomplete` when a
        # provider on the way failed (its attempt says which), so a caller can ask again later.
        # Only a call where no provider answered at all is `route_failed`.
        outcome = "miss" if any(t.outcome == "miss" for t in tried) else "error"
        incomplete = outcome == "miss" and any(t.outcome == "error" for t in tried)
        if incomplete:
            spent = _refund_errors(errored, pending, spent)
        if outcome == "miss":
            body_out = {"output": _empty_output(plan.contract), "raw": None,
                        "_treg": {"served_by": None, "outcome": "miss", "tried": [t.view() for t in tried], "charged_micro": spent,
                                  **({"incomplete": True} if incomplete else {}),
                                  **({"verification": _unchecked("no_hit")} if options.verify else {}),
                                  **({"capped": True} if cost_capped else {}),
                                  **({"dropped": plan.dropped} if plan.dropped else {})}}
            # The event reads the empty answer the caller got, so its counts say 0 rather than
            # nothing (`rows: 0` on a people search that found no one).
            _summarize(parent, hit=False, tried_count=len(tried), output=body_out["output"])
            _audit_parent(parent, ep, 200, spent, audit_client)
            headers = {"X-Treg-Providers-Tried": ",".join(t.provider for t in tried), "X-Treg-Route-Outcome": "miss",
                       **({"X-Treg-Route-Incomplete": "true"} if incomplete else {}),
                       **({"X-Treg-Route-Capped": "true"} if cost_capped else {})}
            return _json(body_out, 200, headers), spent
        _summarize(parent, tried_count=len(tried))
        _audit_parent(parent, ep, 502, 0, audit_client)   # a failed routed call charges nothing
        raise GatewayFailed("route_failed", status_code=502, detail={
            "error": "route_failed", "endpoint_id": ep["id"], "tried": [t.view() for t in tried], "charged_micro": spent,
            "dropped": plan.dropped,
            "message": f"every candidate for {ep['id']} failed; nothing useful was charged" if spent == 0 else
                       f"every candidate for {ep['id']} failed"},
            # every attempt made was cut by treg's own read cap (the rest were skipped, never asked):
            # treg's limit, as on a direct call
            log_kind="response_buffer_limit"
            if too_large and too_large == sum(t.outcome != "skipped" for t in tried) else None)
    cand, doc, output, raw = winner
    served = cand.endpoint["id"]
    winner_outcome = next(t.outcome for t in reversed(tried)
                          if t.endpoint_id == served and t.outcome in ("hit", "weak", "miss"))
    incomplete = False
    if winner_outcome == "miss" and any(t.outcome == "error" for t in tried):
        # The waterfall-off miss: the same rule as an all-miss walk (see the no-winner branch).
        incomplete, spent = True, _refund_errors(errored, pending, spent)
    upgrade, provider = None, cand.endpoint["provider"]
    if options.upgrade and winner_outcome == "hit" and not given.get(options.upgrade):
        remaining = max(0, options.max_cost_micro - spent) if options.max_cost_micro is not None else None
        upgrade, better, upgrade_cost = await _run_upgrade(parent, ep, options.upgrade, output, remaining,
                                                           execute_child, upstream_client)
        spent += upgrade_cost
        if better is not None:
            output, doc, served = better
            provider = served.split(".")[0]
    merged_from: list[str] = []
    if options.merge and len(answers) > 1:
        # A LIST answer is the only shape a union makes sense for, and the caller has already been
        # charged for every attempt (`charged_micro` is the running sum) - so without this the
        # router throws away rows the team paid for.
        field = _list_field(plan.contract)
        if field:
            rows = _merge_rows(field, answers, plan.identity.get("limit"))
            contributed = [c.endpoint["id"] for c, _, core_i, _ in answers if (core_i.get(field) or [])]
            # The deduped union in attempt order whenever two providers contributed: an earlier
            # provider's row for the same person (with its details) leads, and a later provider's
            # own repeats count once, however many rows it returned.
            if len(contributed) > 1:
                output = {**output, field: rows}
                if "count" in plan.contract.output:
                    output["count"] = len(rows)
                merged_from = contributed
                served = merged_from[0]
    # Holders of the asked role now first, past holders last (`row.rank_by_role`), merged or not.
    if plan.contract.row_rank and output and isinstance(output.get(plan.contract.row_of), list):
        output = {**output, plan.contract.row_of: plan.contract.rank_rows(plan.identity, output[plan.contract.row_of])}
    # Every provider whose rows are in the answer: a merged row from one that ignored a filter
    # answers the looser question too.
    ignored = list(dict.fromkeys(k for c, _, core_i, _ in answers
                                 if c.endpoint["id"] in merged_from for k in c.ignored)) if merged_from else list(cand.ignored)
    # The rows answer a LOOSER question than the caller asked when the winner could not express a
    # filter. It is on the attempt, but no caller reads `tried[]` — say it where the answer is, and
    # on a header, or the agent post-filters nothing and never knows why the geography is wrong.
    # A found contact is not a confirmed one. When the contract says so and the provider did not
    # vouch for deliverability (`verified` absent or false — Hunter's `accept_all`, LeadMagic's
    # personal finder, every phone provider), say it where the agent reads the answer. A
    # suggestion only, unless the caller sent `X-Treg-Route-Verify`: then the check runs below.
    advice = (plan.contract.advice_unverified
              if plan.contract.advice_unverified and output and output.get("verified") is not True else "")
    advice_locked = False  # a check's disagree/skip advice wins over domain-mismatch advice
    verification = None
    check = plan.contract.check
    if options.verify and check is not None and winner_outcome in ("hit", "weak"):
        # The find's holds stay open in `pending` while the check runs: a cancellation releases
        # them with everything else, and `run_routed` still closes each exactly once.
        remaining = max(0, options.max_cost_micro - spent) if options.max_cost_micro is not None else None
        verification, check_cost = await _run_check(parent, check, output, remaining,
                                                    pending, execute_child, upstream_client)
        spent += check_cost
        if verification["checked"]:
            advice = ""   # the verdict answers what the advice suggests
            if (check.disagree_advice and output.get("verified") is True and not verification.get("pending")
                    and verification["verdict"] != "valid"):
                advice = check.disagree_advice   # the check wins; say so where the claim is read
                advice_locked = True
        elif check.skip_advice and verification["reason"] == check.skip_reason:
            advice = check.skip_advice   # the caller asked; say why this hit was not checked
            advice_locked = True
    elif options.verify:
        verification = _unchecked("no_hit")   # a miss is never checked; say why
    # Disclose whether the hit's mailbox domain matches the domain the caller sent. Never alter
    # the answer: a LinkedIn-only child that returns a former employer is still a billed hit.
    domain_match = (_email_domain_match(plan.identity, output)
                    if winner_outcome in ("hit", "weak") else None)
    if (domain_match is False and plan.contract.advice_domain_mismatch and not advice_locked):
        advice = plan.contract.advice_domain_mismatch
    confirmation = ({"field": plan.contract.confirm, "confirmed": confirmed_by is not None, "confirmed_by": confirmed_by}
                    if options.confirm and winner_outcome in ("hit", "weak") else None)
    decision = ({"decided": decided_by is not None, "decided_by": decided_by,
                 "ordered_by": "quality" if ordered_on and quality else "price"}
                if options.decisive and winner_outcome in ("hit", "weak") else None)
    # A child (or a merge) that answers more rows than the caller's sent `limit` still returns
    # every billed row: routed endpoints wrap the child's answer and never alter it. Disclose the
    # excess on `_treg` (and a header) instead of trimming. Only a limit the caller SENT counts —
    # a contract default is not a question anyone asked.
    limit_exceeded = None
    asked_limit = plan.identity.get("limit")
    if (isinstance(given, dict) and "limit" in given
            and isinstance(asked_limit, (int, float)) and not isinstance(asked_limit, bool)):
        list_field = _list_field(plan.contract)
        returned_n = len((output or {}).get(list_field) or []) if list_field else 0
        asked_n = int(asked_limit)
        if list_field and returned_n > asked_n:
            limit_exceeded = _limit_exceeded_note(
                asked=asked_n, returned=returned_n, served_by=served,
                merged=bool(merged_from), adapter=cand.adapter)

    output, long_cursor = _long_cursor(output, cand.adapter, bool(merged_from))

    body_out = {"output": output or _empty_output(plan.contract), "raw": doc,
                "_treg": {"served_by": served, "provider": provider, "tier": cand.tier,
                          **({"merged_from": merged_from} if merged_from else {}),
                          **({"next_cursor": long_cursor} if long_cursor else {}),
                          **({"advice": advice} if advice else {}),
                          **({"domain_match": domain_match} if domain_match is not None else {}),
                          "outcome": winner_outcome, "tried": [t.view() for t in tried], "charged_micro": spent,
                          **({"incomplete": True} if incomplete else {}),
                          **({"verification": verification} if verification is not None else {}),
                          **({"confirmation": confirmation} if confirmation is not None else {}),
                          **({"decision": decision} if decision is not None else {}),
                          **({"upgrade": upgrade} if upgrade is not None else {}),
                          **({"ordered_by": "quality" if ordered_on and quality else "price"} if options.quality else {}),
                          **({"capped": True} if cost_capped else {}),
                          **({"ignored_filters": ignored} if ignored else {}),
                          **({"limit_exceeded": limit_exceeded} if limit_exceeded else {}),
                          **({"dropped": plan.dropped} if plan.dropped else {})}}
    _summarize(parent, served_by=served, tried_count=len(tried), output=body_out["output"],
               hit={"hit": True, "weak": True, "miss": False}.get(winner_outcome),
               check_verdict=(verification or {}).get("verdict")
               if (verification or {}).get("checked") else None)
    _audit_parent(parent, ep, 200, spent, audit_client)
    return _json(body_out, 200, {"X-Treg-Served-By": served, "X-Treg-Providers-Tried": ",".join(t.provider for t in tried),
                                 **({"X-Treg-Merged-From": ",".join(merged_from)} if merged_from else {}),
                                 **({"X-Treg-Ignored-Filters": ",".join(ignored)} if ignored else {}),
                                 **({"X-Treg-Limit-Exceeded":
                                     f"asked={limit_exceeded['asked']},returned={limit_exceeded['returned']}"}
                                    if limit_exceeded else {}),
                                 **({"X-Treg-Domain-Match": str(domain_match).lower()} if domain_match is not None else {}),
                                 **({"X-Treg-Route-Capped": "true"} if cost_capped else {}),
                                 **({"X-Treg-Route-Incomplete": "true"} if incomplete else {}),
                                 **({"X-Treg-Route-Confirmed": str(confirmation["confirmed"]).lower()} if confirmation else {}),
                                 **({"X-Treg-Route-Decided": str(decision["decided"]).lower()} if decision else {}),
                                 **({"X-Treg-Route-Upgraded": str(upgrade["upgraded"]).lower()} if upgrade else {}),
                                 "X-Treg-Route-Outcome": winner_outcome}), spent


_CHECK_REASONS = {"route_max_cost": "over_cost_limit", "route_no_candidate": "no_checker",
                  **{k: "spend_limit" for k in _GLOBAL_REFUSALS},
                  **{k: "not_allowed" for k in _CANDIDATE_LOCAL_FAILURES},
                  "org_endpoint_quota_exceeded": "quota_exceeded", "admission_denied": "not_allowed"}


def _unchecked(reason: str) -> dict:
    return {"verdict": "unknown", "checked": False, "served_by": None, "cost_micro": 0, "reason": reason}


async def _run_check(parent: CallContext, check, output: dict, remaining: int | None, pending: list,
                     execute_child, upstream_client) -> tuple[dict, int]:
    """The contract's check on a hit, as an ordinary call: (`_treg.verification`, money it moved).

    A routed check (`treg.people.email.verify`) runs its own waterfall and closes its own holds;
    a direct one leaves its hold in the find's `pending`. Either way the caller's own key comes
    first, its limits and access rules apply, and a failure costs the find nothing: no check ran."""
    cat = catalog_store.load()
    ep = cat.by_id.get(check.endpoint)
    value = output.get(check.field)
    if ep is None or not isinstance(value, str) or not value.strip():
        return _unchecked("no_checker"), 0
    if check.when and not P.evaluate(check.when, {check.field: value}):
        return _unchecked(check.skip_reason), 0
    routed = ep.get("kind") == "routed"
    if routed:
        query, body = {}, {check.field: value}
    else:
        adapter = cat.adapters.get(ep["id"])
        contract = cat.contracts.get(ep.get("capability"))
        if adapter is None or not adapter.verified or contract is None:
            return _unchecked("no_checker"), 0
        identity, variant = canonical_identity(contract, {check.field: value})
        if variant is None:
            return _unchecked("no_checker"), 0
        query, body = adapter.to_upstream(identity, variant)
    prefer = ((PREFER_HEADER.encode(), ",".join(check.prefer).encode()),) if routed and check.prefer else ()
    child = CallContext(input=_child_input(parent, ep, query, body, remaining, prefer),
                        call_ref=f"{parent.call_ref}:v", meta=parent.meta,
                        deferred_settles=None if routed else pending,
                        quota_admissions=parent.quota_admissions,
                        quota_pending=parent.quota_pending)
    try:
        response = await execute_child(child, upstream_client)
        raw = await _read(response)
    except CallFailure as exc:
        return _unchecked(_CHECK_REASONS.get(exc.kind, "checker_failed")), 0
    except Exception:
        log.warning("check %s failed after %s", check.endpoint, parent.call_ref, exc_info=True)
        return _unchecked("checker_failed"), 0
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    if routed:
        meta = (doc.get("_treg") or {}) if isinstance(doc, dict) else {}
        if response.status == 202 and meta.get("outcome") == "pending":
            # Still running after the routed wait (BounceBan `verifying`): its hold stays with the
            # async worker, which settles or releases it; nothing of it is charged now.
            # Forward the child's async poll descriptor so the caller can resume without digging
            # through GET /calls/{call_id}; cost_micro is the hold (reserved), not a settled charge.
            view = {"verdict": "unknown", "checked": True, "served_by": meta.get("served_by"),
                    "cost_micro": int(meta.get("reserved_micro") or 0), "pending": True,
                    "call_id": meta.get("call_ref")}
            if isinstance(meta.get("async"), dict):
                view["async"] = meta["async"]
            return view, 0
        cost = int(_header(response, "X-Treg-Cost-Micro") or 0)
        served = meta.get("served_by")
        if not 200 <= response.status < 300 or meta.get("outcome") not in ("hit", "weak", "miss"):
            return {**_unchecked("checker_failed"), "cost_micro": cost}, cost
        word = (catalog_results.verdict(served, 200, json.dumps(doc.get("raw")).encode())
                if served and meta.get("outcome") != "miss" else None)
        return {"verdict": word or "unknown", "checked": True, "served_by": served, "cost_micro": cost}, cost
    cost = int(_header(response, "X-Treg-Cost-Micro") or 0)
    adapter = cat.adapters[ep["id"]]
    try:
        missed = doc is None or adapter.is_miss(doc)
    except Exception:  # noqa: BLE001 - an unreadable answer is a failed check
        missed = True
    if not 200 <= response.status < 300 or missed:
        return {**_unchecked("checker_failed"), "cost_micro": cost}, cost
    word = catalog_results.verdict(ep["id"], response.status, raw)
    return {"verdict": word or "unknown", "checked": True, "served_by": ep["id"], "cost_micro": cost}, cost


def _name_key(name) -> str | None:
    """First initial and last word, folded: `Bill Gates` and `William H. Gates` are one person's name."""
    words = re.sub(r"[^a-z ]+", " ", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).split() \
        if isinstance(name, str) else []
    return f"{words[0][0]}|{words[-1]}" if len(words) >= 2 else None


async def _run_upgrade(parent: CallContext, ep: dict, key: str, output: dict, remaining: int | None,
                       execute_child, upstream_client) -> tuple[dict, tuple | None, int]:
    """`X-Treg-Route-Upgrade`: the hit named a stronger identity (a LinkedIn URL for an email), so ask
    this same routed endpoint again by that key alone, its providers in order of measured quality.
    Its answer replaces the first only when it is a hit about the same person (the contract's name
    agrees); otherwise the first, already-paid answer stands. Returns (`_treg.upgrade`, the
    replacing (output, raw, served_by) or None, money it moved). A failure costs the first answer nothing."""
    value = output.get(key)
    if not isinstance(value, str) or not value.strip():
        return {"upgraded": False, "via": key, "reason": f"no_{key}"}, None, 0
    child = CallContext(input=_child_input(parent, ep, {}, {key: value}, remaining, ((QUALITY_HEADER.encode(), b"1"),)),
                        call_ref=f"{parent.call_ref}:u", meta=parent.meta, deferred_settles=None)
    try:
        response = await execute_child(child, upstream_client)
        doc = json.loads(await _read(response))
    except CallFailure as exc:
        return {"upgraded": False, "via": key, "reason": exc.kind}, None, 0
    except Exception:
        log.warning("upgrade by %s failed after %s", key, parent.call_ref, exc_info=True)
        return {"upgraded": False, "via": key, "reason": "failed"}, None, 0
    cost = int(_header(response, "X-Treg-Cost-Micro") or 0)
    meta = (doc.get("_treg") or {}) if isinstance(doc, dict) else {}
    view = {"via": key, "served_by": meta.get("served_by"), "cost_micro": cost}
    if not 200 <= response.status < 300 or meta.get("outcome") != "hit":
        return {**view, "upgraded": False, "reason": "no_hit"}, None, cost
    found = doc.get("output") or {}
    if "full_name" in output and _name_key(found.get("full_name")) != _name_key(output.get("full_name")):
        return {**view, "upgraded": False, "reason": "other_name"}, None, cost
    return {**view, "upgraded": True}, ({**output, **{k: v for k, v in found.items() if v is not None}},
                                        doc.get("raw"), meta.get("served_by")), cost


def _note_call(parent: CallContext, plan: Plan, cand: Candidate, child: CallContext, tried: list) -> None:
    """Which order each provider actually reached came from. `route_order` names the first's;
    `route_retry_order` the first later one the contract's own order would not have reached next.
    A child counts only when its request left for the provider or its archived answer was served
    (`reached_provider`), never when a local gate refused it. Each order is read from its first
    candidate not yet passed over, and every earlier attempt is passed over, reached or not."""
    if not child.reached_provider:
        return
    route = dict((parent.route_summary or {}).get("route_rule") or {})
    passed = {t.endpoint_id for t in tried}
    called = cand.endpoint["id"]

    def next_in(order: str) -> str | None:
        return next((e for e in plan.orders.get(order, ()) if e not in passed), None)
    if not plan.orders:
        source = "header"  # the caller's own X-Treg-Route-Prefer order
    else:
        source = "default" if called == next_in("default") else "rule" if called == next_in("rule") else "quality"
    if "route_order" not in route:
        route["route_order"] = source
    elif source not in ("default", "header") and "route_retry_order" not in route:
        route["route_retry_order"] = source
    else:
        return
    _summarize(parent, route_rule=route)


def _summarize(parent: CallContext, **outcome) -> None:
    """What the router decided, for the parent's call event (service.py `_routed_event`)."""
    parent.route_summary = {**(parent.route_summary or {}), **outcome}


def _audit_parent(parent: CallContext, ep: dict, status: int, charged: int, client: str) -> None:
    c = parent.input.caller
    audit.record_call(org_id=c.org_id, user_email=c.email, tool_name=ep["id"], method="POST", path=ep["path"],
                      status_code=status, client=client,
                      api_key_id=c.api_key_id, api_key_name=c.api_key_name,
                      api_key_prefix=c.api_key_prefix,
                      # The tag columns too: a pinned caller's history is filtered on them, so a
                      # routed parent row without its pin would hide the caller's own call.
                      telemetry={"call_ref": parent.call_ref, "endpoint_id": ep["id"], "provider": "treg",
                                 "credential_tier": "routed", "cost_charged_micro": charged,
                                 **_tag_telemetry(parent.meta)})


def _json(value, status: int, headers: dict[str, str]) -> UpstreamResponse:
    body = jsontext.dumps_bytes(value, separators=(",", ":"))

    async def _one():
        yield body

    async def _closed():
        return None
    raw = [(b"content-length", str(len(body)).encode()), (b"content-type", b"application/json")]
    raw += [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return UpstreamResponse(status, tuple(raw), _one(), _closed)
