"""Target resolution and marketplace pricing for proxied calls."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import oauth, oauth_providers
from ... import sandbox as demo_sandbox
from ...config import get_settings, platform_setting_name
from ...domain.capacity import marks as capacity_marks
from ...domain.capacity.routes_view import view as overflow_routes_view
from ...domain.capacity.view import view as capacity_view
from ...domain.catalog import store as catalog_store
from ...domain.connections import authorization as connection_authorization
from ...domain.connections.refresh import expiry_state
from ...domain.governance import access as access_policy
from ...domain.identity.access import Caller
from ...domain.money import settlement as settlement_basis
from ...infra.db import session_maker
from ...models import AsyncResourceRecord, AsyncTaskRecord, CapabilityPin, Org, Secret, Tool
from ..connect import _host_of, _provider_bindings
from .types import ResolutionFailed, ResolvedTarget


AUTHORIZATION_METHOD_HEADER = "X-Treg-Authorization-Method"


@dataclass(frozen=True)
class QueryValues:
    items: tuple[tuple[str, str], ...]

    def get(self, key: str, default=None):
        return next((value for name, value in reversed(self.items) if name == key), default)

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self.items)


def _normalize_scheme(rest: str) -> str:
    """A path param collapses `https://` to `https:/`; restore it."""
    for sch in ("https:/", "http:/"):
        if rest.startswith(sch) and not rest.startswith(sch + "/"):
            return sch + "/" + rest[len(sch):]
    return rest


async def _resolve_call(rest: str, caller: Caller, db: AsyncSession) -> ResolvedTarget:
    """Resolve `/call/<rest>` to (tool, full upstream URL), scoped to the caller's org. Shapes:

    - URL-passthrough (agent-facing): rest is the real upstream URL. Resolve the tool by host
      (indexed) + longest base_url prefix — the caller types no treg vocabulary, just the API.
    - Named (CLI/legacy): rest = "<tool-name>/<upstream-path>".

    Both lookups are constrained to `org_id`, so two orgs resolve independently (and may reuse
    a tool name or an upstream host without colliding).

    Passthrough candidates are additionally filtered by the caller's ACL (project scope AND the
    per-tool list) *before* the longest-prefix tiebreak. That ordering matters: a same-host tool the
    caller cannot use must not be able to cause a 409 — or win the tiebreak — for someone who can't
    even see it in `list_tools`. This narrows the candidate set, so it can never grant access: whatever
    resolves still passes the access-policy gate. The named shape needs no filter (it resolves one tool).
    """
    org_id = caller.org_id
    norm = _normalize_scheme(rest)
    if norm.startswith("http://") or norm.startswith("https://"):
        try:
            host = urlsplit(norm).netloc.lower()
        except ValueError:  # malformed passthrough URL (e.g. unbalanced IPv6 brackets) → 400, not 500
            raise ResolutionFailed(
                "invalid_target", status_code=400, detail="malformed upstream URL")
        on_host = (await db.execute(
            select(Tool).where(Tool.host == host, Tool.org_id == org_id)
        )).scalars().all()
        candidates = [t for t in on_host if access_policy._tool_usable(caller, t)]  # can't use it → can't 409 on it
        # Match on a path-segment boundary, not a raw string prefix: base `.../v2` must NOT match
        # request `.../v20/...` (that would inject v2's credential onto an unregistered sibling path).
        def _prefix_match(base: str) -> bool:
            b = base.rstrip("/")
            return norm == b or norm.startswith(b + "/")

        matches = [t for t in candidates if _prefix_match(t.base_url)]
        if not matches:
            # Tell "no such tool" and "not yours to use" apart. If the ACL filter above is the ONLY
            # reason nothing matched, this is a 403 like the named shape would give — a 404 here
            # would send an admin hunting for a registration that already exists. The message names
            # the HOST the caller already typed, never the internal tool name the ACL hides.
            if any(_prefix_match(t.base_url) for t in on_host):
                raise ResolutionFailed(
                    "tool_access_denied", status_code=403, detail=(
                        f"you don't have access to the registered tool for {host!r} in this team — an "
                        "admin can grant it (dashboard → Team, or `treg org access <you> …`)"))
            raise ResolutionFailed(
                "target_not_found", status_code=404,
                detail=f"no registered tool for upstream {host!r}")
        # Tiebreak on the NORMALIZED length so `.../v1` and `.../v1/` count equal (a real 409), not
        # one silently "longer" than the other.
        longest = max(len(t.base_url.rstrip("/")) for t in matches)
        top = [t for t in matches if len(t.base_url.rstrip("/")) == longest]
        if len(top) > 1:
            # A hand-registered tool for the same API (often predating the OAuth registry, and
            # frequently holding a stale credential) collides on host with the one connect
            # auto-provisioned. Both are real tools, so neither base_url is "longer" — but they are
            # not equally intended: the registry-provisioned one is the live connection the user
            # just authorised, and URL-passthrough is the AGENT-facing mode, so 409-ing here breaks
            # exactly the callers who never typed a tool name. Prefer the provider-backed tool.
            provider_owned = []
            for t in top:
                sids = {b.get("secret_id") for b in (t.bindings or []) if b.get("secret_id") is not None}
                for sid in sids:
                    s = await db.get(Secret, sid)
                    if s is not None and s.org_id == org_id and s.provider:
                        provider_owned.append(t)
                        break
            if len(provider_owned) == 1:
                return ResolvedTarget(provider_owned[0], norm)
            names = ", ".join(repr(t.name) for t in sorted(top, key=lambda t: t.name))
            raise ResolutionFailed(
                "target_ambiguous", status_code=409, detail=(
                    f"ambiguous: multiple tools match {host!r}: {names}; call one by name as "
                    "/call/<name>/<path>"))
        return ResolvedTarget(top[0], norm)

    name, _, path = rest.partition("/")
    tool = (
        await db.execute(select(Tool).where(Tool.name == name, Tool.org_id == org_id))
    ).scalar_one_or_none()
    if tool is None:
        cat = catalog_store.load()
        # A DOTTED name that reached here was meant to be a catalog endpoint id and missed — a
        # near-miss id, most often one segment off. Answering "no tool 'lusha.companies-signals' in
        # this org" describes the wrong half of treg and leaves the caller nothing to try; naming
        # the real id turns the dead end back into the next call.
        if (name not in cat.by_id and "." in name and not path
                and (near := catalog_store.near_ids(name, cat))):
            raise ResolutionFailed(
                "target_not_found", status_code=404, detail={
                    "error": f"no endpoint {name!r} in the catalog",
                    "hint": "did you mean " + ", ".join(near) + "?",
                    "did_you_mean": near})
        detail = f"no tool {name!r} in this org"
        # A caller may have mistaken a catalog-looking operation for a path on the connected own
        # tool. Look only at callable tools inside this org and only on the error path; the first
        # dotted segment is the provider/tool convention (`google-analytics.report` →
        # `google-analytics`). Connection
        # suffixes also count, so an org whose surviving account is `google-analytics-2` still gets
        # an actionable route. Keep catalog_store.near_ids above provider-local and unchanged.
        own_tools = (await db.execute(
            select(Tool).where(Tool.org_id == org_id)
        )).scalars().all()
        first_segment = name.partition(".")[0]
        own_near = sorted({
            t.name for t in own_tools
            if access_policy._tool_usable(caller, t) and (
                name.startswith(t.name + ".")
                or t.name == first_segment
                or t.name.startswith(first_segment + "-")
            )
        }, key=lambda candidate: (-len(candidate), candidate))
        if own_near:
            suggested = own_near[0]
            raise ResolutionFailed(
                "target_not_found", status_code=404, detail={
                    "error": detail,
                    "hint": (f"your org has tool {suggested!r} — call "
                             f"/call/{suggested}/<path>"),
                    "did_you_mean": own_near,
                })
        # A bare provider name (`treg call tikhub /path`) stays a miss, but points at the
        # marketplace form instead of dead-ending — its endpoints are callable without a tool.
        if oauth_providers.get(name) is not None or name in cat.provider_meta:
            detail += (f" — but {name!r} is a marketplace provider; call its endpoints directly: "
                       f"treg catalog search <what you need> → treg call <endpoint-id>")
        raise ResolutionFailed("target_not_found", status_code=404, detail=detail)
    base = tool.base_url.rstrip("/")
    # No path → the base URL itself, WITHOUT a trailing slash: a base pinned to a full resource
    # (e.g. .../v1/charges) must relay as-is — Stripe 404s `/v1/charges/`.
    return ResolvedTarget(tool, (f"{base}/{path.lstrip('/')}" if path else base))


async def resolve_call_target(
    rest: str,
    caller: Caller,
    resolver: Callable[[str, Caller, AsyncSession], Awaitable[ResolvedTarget]],
) -> ResolvedTarget:
    async with session_maker() as db:
        return await resolver(rest, caller, db)


# ---- direct marketplace calls: `treg call <catalog-endpoint-id>`, no tool registration ----------
# See docs/context/interface/cli-audit-2026-07-28.md (design section). The registry stays "our
# stuff"; the catalog is "everything callable". Credential ladder: (1) an org tool bound to the
# provider — resolved via the URL-passthrough shape, so ACLs and ambiguity handling are identical —
# then (2) an org credential matching the provider, injected via a VIRTUAL tool that is never
# persisted (no registry pollution), then (4) TREG'S OWN key for the provider, metered against the
# org's prepaid balance — the keyless first call — and only then (3) an actionable error naming the
# connect/secret fix.
#
# Tier 4 is the only rung that spends OUR money, so it is fenced on every side: the endpoint must be
# `platform_eligible` (priced, price-provenanced, live-verified, not the caller's own account's
# business — see catalog_store.platform_eligible), the provider must be allow-listed AND keyed
# (config.platform_key_for — the kill switch), the org must not be a demo, the estimated cost is
# RESERVED from the balance before the request leaves, and a per-org daily ceiling caps the damage a
# runaway agent can do. The key itself only ever exists as a `platform_setting` NAME in a virtual
# tool's bindings; `relay` reads the value from settings at call time, so no platform credential is
# stored, listable, or reachable from a local run.

def _catalog_endpoint_for(rest: str) -> dict | None:
    """The catalog endpoint `rest` names, or None. Only a dotted, slash-free rest can be an
    endpoint id, so tool names and URL/named shapes never reach the catalog lookup."""
    if "/" in rest or "." not in rest or rest.startswith("http"):
        return None
    return catalog_store.load().by_id.get(rest)


def _enforce_catalog_status(ep: dict) -> None:
    """Refuse a catalog id the provider has retired or broken, with its migration story.

    This runs only after `_resolve_call` has failed and `_catalog_endpoint_for` has identified a
    real catalog id. A team's own tool with the same name therefore still wins, and URL-passthrough
    calls never enter this path at all.
    """
    status = str(ep.get("status") or "").strip().lower()
    if not status:
        return
    detail = f"{ep['id']} is {status}"
    if note := str(ep.get("status_note") or "").strip():
        detail += f": {note}"
    if successor := str(ep.get("superseded_by") or "").strip():
        detail += f" Use {successor} instead."
    elif alternatives := _capability_alternatives(ep):
        # 41 of the 50 TikHub retirements have no same-provider successor, so `superseded_by` has
        # nothing to say for them. A cross-provider sibling is the only help left — and it is the
        # difference between a tombstone and a migration path.
        detail += " " + " ".join(alternatives)
    else:
        detail += " No replacement is currently catalogued."
    raise ResolutionFailed("catalog_retired", status_code=410, detail=detail)


def _authorization_method(secret: Secret) -> str:
    """Stored grant method, including the provider's declared legacy inference."""
    provider = oauth_providers.get(secret.provider) if secret.provider else None
    return (
        provider.authorization_method_name(secret.authorization_method)
        if provider else secret.authorization_method
    )


async def _marketplace_secret(
    service: str, org_id: int, db: AsyncSession, methods: tuple[str, ...] = (),
) -> Secret | None:
    """Tier 2's credential: an org secret tagged with this provider (registry connects), else one
    NAMED exactly for it (`treg secret add tikhub …`). Newest wins — a reconnect supersedes."""
    if methods:
        tagged_rows = (await db.execute(
            select(Secret).where(Secret.org_id == org_id, Secret.provider == service)
            .order_by(Secret.id.desc())
        )).scalars().all()
        # Rows are newest first. Preserve the first row for each method; assigning every row into
        # a comprehension would let the oldest reconnect overwrite the newest one.
        by_method: dict[str, Secret] = {}
        for row in tagged_rows:
            by_method.setdefault(_authorization_method(row), row)
        for method in methods:
            if method in by_method:
                return by_method[method]
        return None
    tagged = (await db.execute(
        select(Secret).where(Secret.org_id == org_id, Secret.provider == service)
        .order_by(Secret.id.desc())
    )).scalars().first()
    if tagged is not None:
        return tagged
    return (await db.execute(
        select(Secret).where(Secret.org_id == org_id, Secret.name == service)
        .order_by(Secret.id.desc())
    )).scalars().first()


@dataclass
class MarketplaceCall:
    """One resolved catalog-endpoint call: where it goes, who paid for the credential, and — when
    treg's own key is paying — what the ledger is holding for it. `call_tool` carries this from
    resolution through the relay to the settle and the telemetry row, so the endpoint id and the
    credential tier are recorded even when the call fails."""

    tool: Tool                      # real (tier 1) or virtual + never persisted (tiers 2/4)
    upstream: str
    consumed: set[str]              # query params eaten by `{placeholder}` path substitution
    endpoint_id: str
    provider: str
    tier: str                       # tool | credential | platform | platform-overflow (child cycle only)
    cost_type: str = ""             # cost.type — decides whether a 4xx is billable (per_call is)
    estimate_micro: int = 0         # RAW provider estimate; the ledger applies the margin
    params_hash: str = ""
    call_id: str | None = None      # the ledger hold, once reserved (metered calls only)
    # The call rides a REGISTRY OAUTH CONNECT of a provider that bills treg's app per use (X's
    # pay-per-use: the app owner pays whoever's token made the call). Orthogonal to `tier` — the
    # credential is genuinely the org's own (tier 1/2), but the upstream bill is ours, so the call
    # is metered anyway. Set by `_billed_marketplace` after the bound secrets are known.
    billed_oauth: bool = False
    unit_micro: int = 0             # RAW per-resource price for a per_result settle-by-count
    # treg's own account is marked exhausted AND an overflow route is enabled: skip the direct
    # attempt (no hold, no vendor 402) and go straight to the child cycle (plan §4 ladder).
    skip_direct: bool = False
    settlement_basis: dict = field(default_factory=dict)
    request_data: dict = field(default_factory=dict)
    async_descriptor: dict | None = None
    resource_ownership: dict | None = None
    # A platform-key utility poll was authorized against this org-owned submission. The buffered
    # response may teach the same row its provider result/file id before the background worker runs.
    async_owner_call_id: str | None = None
    # Admitted through an active capacity lock as its probe (domain.capacity.marks): a 2xx clears
    # exactly that lock.
    probe_lock_id: str | None = None

    @property
    def metered(self) -> bool:
        """True when OUR money is at stake: treg's platform key (tier 4), or an org credential that
        rides treg's pay-per-use OAuth app (`billed_oauth`). Tiers 1/2 on a provider that bills the
        account owner stay unmetered — there the org's own account pays."""
        return self.tier in ("platform", "platform-overflow") or self.billed_oauth


# A `per_result` price is per ROW, so an estimate needs a row count. The caller's own limit param is
# the best available signal; without one, assume a page. Capped, because `limit=100000` must not be
# able to reserve an org's whole balance for a single call — the settle corrects the estimate either way.
_PLATFORM_PAGE_DEFAULT = 20
_PLATFORM_PAGE_MAX = 100
_LIMIT_PARAMS = ("limit", "count", "depth", "page_size", "per_page", "num", "max_results", "size",
                 "pageSize", "perPage", "numResults", "maxResults",
                 "contactsLimit")  # camelCase: companyenrich, exa, lusha; contactsLimit: lusha decision-makers


# Units that name an INPUT entity rather than a returned row: the caller pays per thing they asked
# about (an SE Ranking `target`, a Serpstat `domain`, a `keyword`; `call` is the flat case). Providers
# billing this way rarely report a per-call cost, so the reserve IS the charge — a wrong count is a
# wrong bill, not a hold the settle trues up.
_ENTITY_UNITS = frozenset({"target", "domain", "keyword", "call"})
_ENTITY_KEYS = ("targets", "keywords", "domains", "urls", "target", "keyword", "domain", "url")
_ENTITY_MAX = 10_000  # a body cannot reserve more than this many entities' worth in one call


def _doc_entities(doc) -> int:
    """Entities named by one JSON object: a list under an entity key (top level, or inside a
    JSON-RPC `params` — serpstat), else one for a scalar target."""
    if not isinstance(doc, dict):
        return 0
    for scope in (doc, doc.get("params")):
        if not isinstance(scope, dict):
            continue
        for key in _ENTITY_KEYS:
            val = scope.get(key)
            if isinstance(val, list):
                return len(val)
            if isinstance(val, str) and val.strip():
                return 1
    return 0


def _entity_count(query, body: bytes) -> int:
    """How many billable input entities a request names. Query first (repeated keys — `target=a&
    target=b` or `targets[]=` — and comma-separated values both count), then the JSON body (an
    entity array, or one per task object in a DataForSEO-style array). Never below one: a request
    that names no entity still asks about the one its path implies."""
    n = 0
    if query is not None:
        items = query.multi_items() if hasattr(query, "multi_items") else list(query.items())
        for key, val in items:
            if key.rstrip("[]") in _ENTITY_KEYS and val is not None and str(val).strip():
                n += max(1, len([p for p in str(val).split(",") if p.strip()]))
    if n == 0 and body:
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            doc = None
        if isinstance(doc, list):
            n = sum(_doc_entities(d) for d in doc) or len(doc)
        else:
            n = _doc_entities(doc)
    return max(1, min(n, _ENTITY_MAX))


def _body_limit(body: bytes) -> int | None:
    """A row-count signal from a JSON body: an explicit limit key first (dataforseo takes
    `[{..., "limit": 3}]`, lusha `{"limit": 1}`), else the ARRAY LENGTH — providers that take a
    list of inputs (brightdata's urls, dataforseo's tasks) bill one result per item, so a 1-item
    body estimating at the 20-row default overstated 20x (seen live: $0.03 shown for a $0.0015
    call). Under-estimating is safe either way — the settle trues up, overruns included."""
    if not body:
        return None
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    items = None
    if isinstance(doc, list) and doc:
        items = len(doc)
        doc = doc[0]
    if not isinstance(doc, dict):
        return items
    for name in _LIMIT_PARAMS:
        val = doc.get(name)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    for name in ("targets", "keywords", "domains", "urls", "lookups", "emails"):
        val = doc.get(name)  # one row per item: moz targets, dataforseo keywords, companyenrich domains, brightdata urls
        if isinstance(val, list) and val:
            return len(val)
    # icypeas / lusha: {"pagination": {"size": 10}}; influencersclub: {"paging": {"limit": 10}} —
    # the miss on `paging` left discovery reserving the 20-row default whatever the caller asked
    # (found live 2026-08-30, the same calls whose settle over-billed).
    for envelope in ("pagination", "paging"):
        nested = doc.get(envelope)
        if isinstance(nested, dict):
            for name in _LIMIT_PARAMS:
                val = nested.get(name)
                if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                    return val
    return items


def _platform_estimate_micro(cost: dict, query, body: bytes = b"") -> int:
    """What one call is expected to cost the platform, in RAW micro-USD (no margin — ledger.reserve
    applies that). Rounds UP: a fraction of a micro-dollar is not representable and must not round to
    free. Returns 0 for a genuinely free endpoint, which reserves nothing."""
    usd = cost.get("usd")
    if usd is None:
        return 0
    n = 1
    if cost.get("type") in ("per_result", "quota_rows") and cost.get("unit") in _ENTITY_UNITS:
        # Priced per INPUT entity, not per returned row: the page-size default below has no
        # meaning here and billed one-target calls 20x (seranking summary, serpstat overview —
        # 2026-09-05). The request names how many entities it asks about.
        n = 1 if cost.get("unit") == "call" else _entity_count(query, body)
    elif cost.get("type") in ("per_result", "quota_rows"):
        asked = None
        for name in _LIMIT_PARAMS:
            raw = query.get(name)
            if raw is not None and str(raw).strip().isdigit():
                asked = int(str(raw).strip())
                break
        if asked is None:
            asked = _body_limit(body)  # POST providers put the row count in the body, not the query
        n = max(1, min(asked or _PLATFORM_PAGE_DEFAULT, _PLATFORM_PAGE_MAX))
    # Round to 9 dp BEFORE the ceil: float artifacts (0.0015 × 3 → 4500.000000001) must not
    # over-reserve a phantom micro-dollar.
    raw_micro = round(usd * n * 1_000_000, 9)
    whole = int(raw_micro)
    return whole + 1 if raw_micro > whole else whole


# ---- oauth-billed metering: providers whose upstream bill lands on treg's app -------------------
# X moved to pay-per-use (Feb 2026): the APP OWNER is billed per resource read / per post written,
# whoever's user token made the call. A registry connect rides treg's app, so those calls spend
# treg's prepaid credits and must be metered against the org's balance — the same reserve→settle
# path as tier 4. A BYO connect (/oauth/start with the caller's own client_id) stores
# `secret.provider == ""` and is therefore never flagged: its upstream bill is already the org's.

def _usd_to_micro(usd: float) -> int:
    """USD → RAW micro-USD, rounded UP like `_platform_estimate_micro` — a fraction of a
    micro-dollar must not round to free."""
    raw = round(usd * 1_000_000, 9)
    whole = int(raw)
    return whole + 1 if raw > whole else whole


def _truthy(value) -> bool:
    """Provider query/body booleans arrive as strings or JSON booleans; interpret both."""
    return value is True or (isinstance(value, str) and value.strip().lower() in ("1", "true", "yes"))


def _json_object(body: bytes) -> dict:
    try:
        doc = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _input_count(doc: dict, keys: tuple[str, ...]) -> int:
    """Count request records without mistaking field-selection arrays for billable inputs."""
    sizes = [len(doc[k]) for k in keys if isinstance(doc.get(k), list)]
    return max(sizes, default=1)


def _credit_modifiers(cost: dict, query, doc: dict) -> tuple[bool, float, float, float]:
    """Return (free, reserve add, settle add, add per requested result) from catalog rules.

    The request SHAPE stays provider-aware, but every credit NUMBER stays in the provider YAML.
    A documented but live-unbilled rider can stay in the safety hold with `reserve_only: true`.
    This prevents a rate-card edit from leaving hardcoded arithmetic in the billing path.
    """
    free, added, settled_added, per_result = False, 0.0, 0.0, 0.0
    modifiers = cost.get("modifiers")
    if not isinstance(modifiers, dict):
        return free, added, settled_added, per_result
    for name, rule in modifiers.items():
        if not isinstance(rule, dict):
            continue
        location = rule.get("location", "query")
        if location == "query":
            values = [query.get(name)]
        elif location == "body":
            values = [doc.get(name)]
        elif location == "lookups":
            lookups = doc.get("lookups") if isinstance(doc.get("lookups"), list) else []
            values = [item.get(name) for item in lookups if isinstance(item, dict)]
        else:
            continue
        when = rule.get("when", "truthy")
        matches = (any(value not in (None, "") for value in values)
                   if when == "present" else any(_truthy(value) for value in values))
        if not matches:
            continue
        if rule.get("set_credits") == 0:
            free = True
        if isinstance(rule.get("add_credits"), (int, float)):
            added += float(rule["add_credits"])
            if not rule.get("reserve_only"):
                settled_added += float(rule["add_credits"])
        if isinstance(rule.get("add_credits_per_result"), (int, float)):
            per_result += float(rule["add_credits_per_result"])
    return free, added, settled_added, per_result


def _marketplace_pricing(
    provider: str, endpoint_id: str, cost: dict | None, query, body: bytes
) -> tuple[int, int]:
    """Return (reserve estimate, response-count unit), in raw micro-USD.

    The catalog remains the price source. This helper only models provider rules that one fixed
    scalar cannot express: Crustdata batch-shaped single calls and Aviato preview/add-on/bulk modes.
    `unit` is non-zero only when the response must decide the final charge.
    """
    if not cost:
        return 0, 0
    estimate = _platform_estimate_micro(cost, query, body)
    unit = (_usd_to_micro(cost["usd"])
            if cost.get("type") in ("per_result", "quota_rows") and cost.get("usd") else 0)
    if provider == "crustdata" and endpoint_id in (
        "crustdata.companies.enrich", "crustdata.people.enrich"
    ):
        doc = _json_object(body)
        count = _input_count(doc, (
            "domains", "names", "professional_network_profile_urls", "business_emails"
        ))
        return _usd_to_micro(float(cost.get("usd") or 0) * count), unit
    if provider != "aviato":
        return estimate, unit

    rate = catalog_store.load().credit_rates.get("aviato")
    if not rate:
        return estimate, unit
    def credit_micro(credits):
        return _usd_to_micro(float(credits) * rate)

    doc = _json_object(body)
    free, added, settled_added, per_result = _credit_modifiers(cost, query, doc)
    if free:
        return 0, 0
    credits = float(cost.get("value") or 0) + added
    settled_credits = float(cost.get("value") or 0) + settled_added
    if endpoint_id in ("aviato.companies.enrich.bulk", "aviato.people.enrich.bulk"):
        lookups = doc.get("lookups") if isinstance(doc.get("lookups"), list) else []
        per_record = credit_micro(credits)
        return per_record * max(1, len(lookups)), credit_micro(settled_credits)
    if per_result:
        raw = query.get("perPage")
        asked = int(raw) if raw is not None and str(raw).isdigit() else _PLATFORM_PAGE_DEFAULT
        asked = max(1, min(asked, _PLATFORM_PAGE_MAX))
        # The documented rider stays in the safety hold. A catalog `settle: base` rule can release
        # it after the response when multi-row balance evidence proves that the provider did not
        # charge or deliver the add-on.
        return credit_micro(credits + asked * per_result), 0
    if cost.get("modifiers"):
        settle_unit = credit_micro(settled_credits) if added != settled_added else 0
        return credit_micro(credits), settle_unit
    return estimate, 0


def _oauth_billed_provider(secrets: dict[int, Secret]):
    """The flagged OAuthProvider whose registry connect this call's bindings ride, or None.
    Three gates: the secret is a REGISTRY connect (`secret.provider` is only ever set by the
    callback of a provider-mode /oauth/start — BYO connects carry ""), the registry entry says the
    upstream bills treg's app (`platform_billed`), and this deployment opted into charging
    (`TREG_OAUTH_BILLED_PROVIDERS`, the kill switch — empty keeps today's free behavior)."""
    billed = get_settings().oauth_billed_set
    if not billed:
        return None
    for s in secrets.values():
        if s.kind == "oauth" and s.provider and s.provider in billed:
            p = oauth_providers.get(s.provider)
            if p is not None and p.platform_billed:
                return p
    return None


def _billed_endpoint_match(service: str, method: str, path: str) -> dict | None:
    """The catalog endpoint a URL-passthrough call to `path` lands on, or None. Exact-path entries
    win over templated ones ({id} → one segment), so `/2/users/me` matches the own-account read and
    not `/2/users/{id}`. Purely for pricing + telemetry — never for routing."""
    best, best_placeholders = None, 99
    for ep in catalog_store.load().by_id.values():
        if ep.get("provider") != service or (ep.get("method") or "GET").upper() != method:
            continue
        template = ep.get("path") or "/"
        placeholders = template.count("{")
        if placeholders >= best_placeholders:
            continue
        pattern = re.sub(r"\{\w+\}", "[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}"))
        if re.fullmatch(pattern, path):
            best, best_placeholders = ep, placeholders
    return best


def _post_has_link(body: bytes) -> bool:
    """Whether a write body's `text` carries a URL — X prices those at `billed_write_link_usd`
    (13x a plain post). Sniffs only the text field, not the whole body, so a quote-post id or a
    docs URL in some other field can't inflate the price."""
    if not body:
        return False
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    text = doc.get("text") if isinstance(doc, dict) else None
    return bool(isinstance(text, str) and re.search(r"https?://|www\.", text))


def _oauth_billed_estimate(provider, ep: dict | None, method: str, query, body: bytes) -> tuple[int, str, int]:
    """What this oauth-billed call is expected to cost, RAW micro-USD (ledger applies the margin)
    → (estimate_micro, cost_type, unit_micro). A priced catalog entry wins (the curated x.yaml
    carries per-endpoint rates: own-account reads are 5x cheaper, user lookups 2x dearer than the
    default); the provider-level rates cover the extended/passthrough long tail. `unit_micro` is
    the per-resource price a `per_result` settle counts the response against."""
    cv = catalog_store.load().cost_view(ep.get("cost"), provider.service) if ep and ep.get("cost") else None
    # A ZERO price must fall through to the provider rate, not bill zero — on an oauth-billed
    # provider the upstream charges us whatever the catalog says, so `free` there is a catalog bug
    # (a stale ingest), never a fact. Spelled out because it used to ride on `0.0` being falsy:
    # the same expression read as "no price recorded" and "the price is nothing", and the catalog
    # could publish free while the balance was debited the fallback.
    if cv and cv.get("type") != "free" and cv.get("usd"):
        ctype = str(cv.get("type") or "per_call")
        est = _platform_estimate_micro(cv, query, body)
        if method != "GET" and provider.billed_write_link_usd and _post_has_link(body):
            est = max(est, _usd_to_micro(provider.billed_write_link_usd))
        return est, ctype, (_usd_to_micro(cv["usd"]) if ctype in ("per_result", "quota_rows") else 0)
    if method == "GET":
        rate = provider.billed_read_usd
        est = _platform_estimate_micro({"type": "per_result", "usd": rate}, query, body)
        return est, "per_result", _usd_to_micro(rate)
    if provider.billed_write_link_usd and _post_has_link(body):
        return _usd_to_micro(provider.billed_write_link_usd), "per_call", 0
    return _usd_to_micro(provider.billed_write_usd), "per_call", 0


async def _billed_marketplace(
    mk: MarketplaceCall | None, provider, tool: Tool, upstream_url: str, *, method: str,
    query: QueryValues, has_body: bool, read_body: Callable[[], Awaitable[bytes]],
) -> MarketplaceCall:
    """Flag (or, for a URL-passthrough call, build) the `MarketplaceCall` that meters an
    oauth-billed relay. The catalog id shape arrives with an `mk` (tier 1/2 — keep its endpoint id
    and telemetry identity); the passthrough shape gets one made here, priced off the catalog
    entry its path lands on so both shapes pay the same price for the same route."""
    body = await read_body() if has_body else b""
    method = method.upper()
    if mk is None:
        path = urlsplit(upstream_url).path or "/"
        ep = _billed_endpoint_match(provider.service, method, path)
        endpoint_id = ep["id"] if ep else f"{provider.service}.passthrough"
        mk = MarketplaceCall(
            tool=tool, upstream=upstream_url, consumed=set(), endpoint_id=endpoint_id,
            provider=provider.service, tier="tool",
            params_hash=_params_hash(endpoint_id, query.multi_items(), body))
    else:
        ep = catalog_store.load().by_id.get(mk.endpoint_id)
    est, ctype, unit = _oauth_billed_estimate(provider, ep, method, query, body)
    mk.billed_oauth, mk.estimate_micro, mk.cost_type, mk.unit_micro = True, est, ctype, unit
    # OAuth-app billing can override a catalog estimate (notably X writes containing a URL), so its
    # response-time basis must be rebuilt from the authoritative billed-app estimate.
    mk.settlement_basis = {
        "when": "response", "amount": {"kind": "observed"},
        "fallback_micro": est, "reserve_micro": est,
    }
    return mk


def _params_hash(endpoint_id: str, query_items: list[tuple[str, str]], body: bytes) -> str:
    """An identity for "this exact call again": sha256 over the endpoint id, the ORDER-INDEPENDENT
    query, and a digest of the body. The body itself is never stored or logged — only its hash — so
    this is safe to keep forever and is the future cache key (plan phase 5, repeat-rate measurement)."""
    h = hashlib.sha256()
    h.update(endpoint_id.encode("utf-8", "replace"))
    for k, v in sorted(query_items):
        h.update(b"\x1f" + f"{k}={v}".encode("utf-8", "replace"))
    h.update(b"\x1e" + (hashlib.sha256(body).digest() if body else b""))
    return h.hexdigest()


def _platform_bindings(provider) -> list[dict]:
    """Tier 4's injection: the SAME header/param shape a pasted key of this provider gets
    (`_provider_bindings`), except the value is named rather than carried — `relay` reads
    `platform_setting` from settings at call time. That is the whole security model: treg's key is
    never written to a Secret row (unreadable by the tenant, unexportable by a local run, and
    `api.py`'s cross-org secret check would reject it anyway)."""
    setting = platform_setting_name(provider.service)
    if provider.token_location == "query":
        bindings = [{"platform_setting": setting, "injector": "env", "location": "query",
                     "name": provider.token_param, "format": provider.token_format}]
    else:
        bindings = [{"platform_setting": setting, "injector": "env", "location": "header",
                     "name": provider.token_header, "format": provider.token_format}]
    # Keep tier 4 protocol-identical to BYOK. Required provider headers are constants, but they
    # still use the same platform setting reference so the normal binding validator and injector
    # own the whole shape. Crustdata's x-api-version pin is the first provider that needs this.
    source = {k: v for k, v in bindings[0].items()
              if k in ("platform_setting", "injector", "secret_field")}
    bindings.extend({**source, "location": "header", "name": name, "format": value}
                    for name, value in provider.required_headers)
    # A per-user credential PAIR (Tomba's key+secret headers) needs treg's own second half on
    # tier 4. platform_extra_setting is tier-4-only by design: extra_credential_setting would also
    # ride user connects, pairing a user's key with treg's secret — a pair the provider rejects.
    if provider.needs_extra_credential and provider.platform_extra_setting:
        bindings.append({"platform_setting": provider.platform_extra_setting, "injector": "env",
                         "location": "header", "name": provider.extra_credential_header,
                         "format": "{secret}"})
    return bindings


def _platform_offer(ep: dict, provider, org: Org) -> dict | None:
    """May tier 4 serve `ep` for this org, and at what price? The cost view when yes, None when no.

    Every clause is a refusal we WANT to be boring: an unpriced/unknown-confidence price
    (`platform_eligible`), a provider nobody enabled (`platform_key_for` — key AND allow-list), an
    OAuth provider (a platform key is meaningless for one: the credential is a user's own account),
    or a demo org (the sandbox and the public demo must never be able to spend real money — the
    landing page is reachable by anyone with the URL)."""
    if not provider.uses_pasted_secret:
        return None
    cat = catalog_store.load()
    if not cat.platform_eligible(ep):
        return None
    if not get_settings().platform_key_for(ep["provider"]):
        return None
    if demo_sandbox.is_sandbox(org) or org.public_demo:
        return None
    return cat.cost_view(ep.get("cost"), ep["provider"]) or None


def _capability_alternatives(ep: dict, *, limit: int = 3) -> list[str]:
    """Other providers' endpoints for the same capability, best first — derived, never hand-written.

    A dead end that names only the provider the caller asked for is the reason one org spent 268
    calls on `meta-ad-library.meta-ads.library.search` while `scrapecreators.…-search-ads` — the
    same `capability` string, on a key treg already holds — sat one row away answering 192 of 208
    calls for fourteen other teams. The refusal knew the capability the whole time.

    Read from `cat.endpoints`, which `_parse` has already stripped of marked rows, so a retirement
    stops being suggested the moment it is marked and no list here needs maintaining. This
    COMPARES, it does not route: treg never fails over on the caller's behalf (see the charter),
    so this names the options and their prices and leaves the choice where it belongs.

    Deliberately synchronous and I/O-free. Measured success would need `endpoint_stats.observed`
    and a DB round-trip on an error path — which is how a 404 turns into a 500 — and the caller's
    next step, `catalog get`, already ranks the same siblings by observed success.
    """
    capability = ep.get("capability")
    if not capability:  # only curated capabilities can find siblings; nothing is better than a guess
        return []
    cat = catalog_store.load()
    settings = get_settings()
    ranked = []
    for alt in cat.for_capability(capability):
        if alt["id"] == ep["id"]:
            continue
        cost = cat.cost_view(alt.get("cost"), alt["provider"])
        usd = cost.get("usd") if cost else None
        # "Servable" is the caller's real question: not "does another row exist" but "can treg
        # answer it for me right now". Both halves of tier 4, exactly as `_platform_offer` asks.
        servable = bool(cat.platform_eligible(alt) and settings.platform_key_for(alt["provider"]))
        ranked.append((not servable, usd if usd is not None else float("inf"), alt["id"], usd, servable))
    if not ranked:
        return []
    ranked.sort()
    lines = [f"another provider serves {capability}:"]
    for _, _, alt_id, usd, servable in ranked[:limit]:
        price = "price unknown" if usd is None else ("free" if usd == 0 else f"~${usd:g}/call")
        how = "callable now on treg's key" if servable else f"needs your own {alt_id.split('.')[0]} credential"
        lines.append(f"  {alt_id}  {price}  ({how})")
    return lines


def _marketplace_no_credential(
    service: str, ep_id: str, provider, ep: dict | None = None,
) -> ResolutionFailed:
    """Tier 3: the actionable dead-end. Every line names a real command; a pasted-key provider
    gets the `secret add` route too (name it for the service so the ladder finds it)."""
    lines = [f"no {service} credential in this org — {ep_id} is a marketplace endpoint"]
    lines.append(f"  connect one:  treg connections connect --provider {service}")
    if provider.uses_pasted_secret:
        lines.append(f"  or add a key: treg secret add {service} --env-var {service.upper().replace('-', '_')}_API_KEY")
    lines.append(f"  or register the tool yourself: treg tool add {service} --base-url {provider.base_url} …")
    if ep is not None:
        lines.extend(_capability_alternatives(ep))
    return ResolutionFailed(
        "credential_missing", status_code=404, detail="\n".join(lines))


_VALID_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _marketplace_upstream(
    ep: dict, provider, query_params, authorization_method: str = "",
) -> tuple[str, set[str]]:
    """The full upstream URL for an endpoint-id call, with `{placeholder}` path params filled from
    the caller's query params (they are consumed — dropped from the relayed query). Missing
    required params fail HERE, before a credential is touched or money spent."""
    path = (ep.get("authorization_paths") or {}).get(authorization_method) or ep["path"] or "/"
    inp = ep.get("input") or {}
    for where in ("pathParams", "queryParams"):
        for name, spec in (inp.get(where) or {}).items():
            allowed = tuple((spec or {}).get("authorization_methods") or ())
            if (authorization_method and allowed and authorization_method not in allowed
                    and query_params.get(name) is not None):
                raise ResolutionFailed(
                    "catalog_parameter_invalid", status_code=400, detail=(
                        f"{ep['id']} does not accept {name} with {authorization_method}; "
                        f"choose {', '.join(allowed)} or remove {name}"))
    consumed = set()
    for name in re.findall(r"{(\w+)}", path):
        value = query_params.get(name)
        if value is None:
            raise ResolutionFailed(
                "catalog_parameter_invalid", status_code=400, detail=(
                    f"{ep['id']} needs --query {name}=<value> "
                    f"(a path parameter of {ep['path']})"))
        # Agents often pass `siteUrl` straight from GSC's sites list, where it may already be
        # encoded. Preserve a value containing a real %HH escape; otherwise encode it exactly once.
        # A literal/invalid percent sequence has no valid escape and therefore becomes `%25`.
        rendered = value if _VALID_PERCENT_ESCAPE_RE.search(value) else quote(value, safe="")
        path = path.replace("{%s}" % name, rendered)
        consumed.add(name)
    required = [k for k, v in (inp.get("queryParams") or {}).items()
                if isinstance(v, dict) and v.get("required")
                and (not v.get("authorization_methods")
                     or authorization_method in v["authorization_methods"])
                and query_params.get(k) is None]
    if required:
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400, detail=(
                f"{ep['id']} requires --query "
                + " --query ".join(f"{k}=<value>" for k in required)))
    return provider.base_url.rstrip("/") + "/" + path.lstrip("/"), consumed


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(body: bytes, ep_id: str) -> dict:
    """Parse a platform request without accepting ambiguous duplicate JSON keys."""
    def object_pairs(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise _DuplicateJsonKey(str(name))
            result[name] = value
        return result

    try:
        document = json.loads(body, object_pairs_hook=object_pairs)
    except _DuplicateJsonKey as exc:
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400,
            detail=f"{ep_id} request body repeats JSON field {str(exc)!r}",
        ) from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400,
            detail=f"{ep_id} requires a JSON request body",
        ) from None
    if not isinstance(document, dict):
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400,
            detail=f"{ep_id} requires a JSON object request body",
        )
    return document


def _input_spec(input_schema: dict, dotted: str) -> dict | None:
    """Find a catalog body-field spec across the direct-map and nested-properties shapes."""
    current: object = input_schema.get("body") or {}
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        if part in current:
            current = current[part]
        else:
            current = (current.get("properties") or {}).get(part)
    return current if isinstance(current, dict) else None


def _document_value(document: object, dotted: str) -> object:
    current = document
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _enforce_platform_pricing_selectors(ep: dict, body: bytes) -> None:
    """Bind a platform-priced row to its fixed request discriminator before reserve/relay.

    Catalog tables may price several rows on one upstream path. A table condition whose body field
    has a singleton enum is the row identity, not caller choice: accepting another value lets a cheap
    row reserve for an expensive model. Full schema validation remains out of the faithful BYOK path.
    """
    input_schema = ep.get("input") or {}
    selectors: dict[str, object] = {}
    for row in (ep.get("cost") or {}).get("table") or []:
        for path in (row.get("when") or {}):
            if not str(path).startswith("body."):
                continue
            relative = str(path)[len("body."):]
            spec = _input_spec(input_schema, relative)
            allowed = spec.get("enum") if spec else None
            if isinstance(allowed, list) and len(allowed) == 1:
                selectors[relative] = allowed[0]
    if not selectors:
        return
    document = _strict_json_object(body, ep["id"])
    for path, expected in sorted(selectors.items()):
        actual = _document_value(document, path)
        if actual != expected:
            raise ResolutionFailed(
                "catalog_parameter_invalid", status_code=400, detail={
                    "error": "catalog_parameter_invalid",
                    "endpoint_id": ep["id"],
                    "parameter": f"body.{path}",
                    "expected": expected,
                    "message": (
                        f"{ep['id']} fixes body.{path} to {expected!r}; "
                        "choose the catalog endpoint for the requested value"
                    ),
                },
            )


def _async_resource_refs(ep: dict) -> list[tuple[str, dict]]:
    """How this utility is referenced by effective async descriptors in the live catalog."""
    refs: list[tuple[str, dict]] = []
    for candidate in catalog_store.load().endpoints:
        if candidate.get("provider") != ep.get("provider"):
            continue
        descriptor = candidate.get("async") or {}
        poll = descriptor.get("poll") or {}
        if poll.get("endpoint") == ep.get("id") and isinstance(poll.get("param"), dict):
            refs.append(("poll", poll["param"]))
        result = descriptor.get("result") or {}
        if result.get("fetch") == ep.get("id") and isinstance(result.get("fetch_param"), dict):
            refs.append(("fetch", result["fetch_param"]))
    return refs


def _one_resource_value(ep: dict, query: QueryValues, refs: list[tuple[str, dict]]) -> str:
    supplied: list[str] = []
    for _, param in refs:
        name = str(param.get("name") or "")
        supplied.extend(value for key, value in query.items if key == name)
    values = set(supplied)
    if len(values) != 1:
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400,
            detail=f"{ep['id']} requires exactly one unambiguous async resource id",
        )
    return values.pop()


def _descriptor_ref(descriptor: dict, kind: str) -> tuple[str, dict]:
    if kind == "poll":
        rule = descriptor.get("poll") or {}
        return str(rule.get("endpoint") or ""), rule.get("param") or {}
    rule = descriptor.get("result") or {}
    return str(rule.get("fetch") or ""), rule.get("fetch_param") or {}


async def _enforce_platform_async_ownership(
    ep: dict, query: QueryValues, caller: Caller, db: AsyncSession,
) -> str | None:
    """Authorize shared-key task/result utilities through the caller org's durable submission."""
    ownership = ep.get("resource_ownership") or {}
    required = ownership.get("requires") or {}
    resource_owned = False
    if required:
        value = _one_resource_value(ep, query, [("resource", {"name": required.get("param")})])
        resource_owned = (await db.execute(select(AsyncResourceRecord.id).where(
            AsyncResourceRecord.org_id == caller.org_id,
            AsyncResourceRecord.provider == ep["provider"],
            AsyncResourceRecord.resource_kind == required.get("kind"),
            AsyncResourceRecord.resource_id == value,
        ))).scalar_one_or_none() is not None

    refs = _async_resource_refs(ep)
    if not refs:
        if required and not resource_owned:
            raise _async_resource_denied()
        return None
    value = _one_resource_value(ep, query, refs)
    candidates = (await db.execute(select(AsyncTaskRecord).where(
        AsyncTaskRecord.org_id == caller.org_id,
        AsyncTaskRecord.provider == ep["provider"],
        or_(AsyncTaskRecord.task_id == value, AsyncTaskRecord.result_id == value),
    ))).scalars().all()
    for row in candidates:
        for kind, current_param in refs:
            endpoint_id, frozen_param = _descriptor_ref(row.descriptor or {}, kind)
            if endpoint_id != ep["id"] or frozen_param != current_param:
                continue
            if kind == "poll" and row.task_id == value:
                return row.call_id
            if kind == "fetch":
                result = (row.descriptor or {}).get("result") or {}
                same_as_task = (
                    row.task_id == value
                    and (result.get("fetch_param") or {}).get("value_from")
                    == (row.descriptor or {}).get("id_from")
                )
                if row.result_id == value or same_as_task:
                    return None
    if resource_owned:
        return None
    raise _async_resource_denied()


def _async_resource_denied() -> ResolutionFailed:
    return ResolutionFailed(
        "async_resource_not_owned", status_code=403, detail={
            "error": "async_resource_not_owned",
            "message": "this async task or result is not available to the current team",
        },
    )


async def _enforce_capability_pin(ep: dict, caller: Caller, db: AsyncSession) -> None:
    """Refuse a catalog call that goes around the team's pin for that capability.

    A pin is a decision the team already made ("for finding work emails we use Hunter"), so the
    answer names the endpoint they DO use — an agent that gets told "no" without being told "use
    this instead" will simply try the next provider and be refused again.

    Enforced here rather than in the client so it does not depend on the caller's goodwill, and
    before anything is reserved, so a refusal never has to un-hold money."""
    cap = ep.get("capability")
    if not cap or caller.org_id is None:
        return
    pin = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == caller.org_id,
        CapabilityPin.capability == cap).order_by(CapabilityPin.id))).scalars().first()
    if pin is None or pin.provider == ep["provider"]:
        return
    cat = catalog_store.load()
    # Suggest the OBVIOUS endpoint, not merely the first one in file order: `core` is the curated
    # route for a job, `extended` is the bulk-ingested long tail. Suggesting
    # `tikhub.x.tiktok-analytics-fetch-creator-info-and-milestones` when `tikhub.tiktok.user.profile`
    # exists reads as a broken suggestion and sends the caller somewhere they did not ask to go.
    mine = [e for e in cat.for_capability(cap) if e["provider"] == pin.provider]
    mine.sort(key=lambda e: ((e.get("tier") or "") != "core", not cat.platform_eligible(e), e["id"]))
    alt = mine[0]["id"] if mine else None
    raise ResolutionFailed(
        "capability_pinned", status_code=403, detail={
            "error": "capability_pinned",
            "message": (f"this team uses {pin.provider!r} for {cap!r}"
                        + (f" — call {alt} instead" if alt else "")
                        + f". An admin can change it: treg org unpin {cap}"),
            "capability": cap, "pinned_provider": pin.provider, "use_endpoint": alt,
        })


async def _provider_tool_grant(
    service: str, methods: tuple[str, ...], caller: Caller, db: AsyncSession,
    endpoint: dict | None = None,
) -> tuple[Tool, Secret, str] | None:
    """Resolve a named catalog endpoint by provider and grant identity, not only by host.

    This is the generic fix for providers that share one upstream host. It stays in catalog-call
    resolution; the faithful relay still receives one resolved tool and knows no provider rules.
    """
    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    secrets = (await db.execute(select(Secret).where(
        Secret.org_id == caller.org_id, Secret.provider == service,
    ))).scalars().all()
    secrets_by_id = {secret.id: secret for secret in secrets}
    provider = oauth_providers.get(service)
    connection_names = {
        item.name: item.connection_name for item in (provider.authorization_methods if provider else ())
    }
    matches: list[tuple[bool, int, bool, int, Tool, Secret, str]] = []
    denied = False
    for tool in tools:
        for binding in tool.bindings or []:
            sid = binding.get("secret_id")
            if sid is None:
                continue
            secret = secrets_by_id.get(sid)
            if secret is None:
                continue
            method = _authorization_method(secret)
            if method not in methods:
                continue
            if not access_policy._tool_usable(caller, tool):
                denied = True
                continue
            priority = methods.index(method)
            exact = tool.name == connection_names.get(method, service)
            authorization = (
                connection_authorization.method_spec(provider, method) if provider else None
            )
            required = (
                connection_authorization.required_scopes(endpoint, authorization)
                if endpoint else []
            )
            granted = set(secret.granted_scopes.split())
            scope_gap = any(scope not in granted for scope in required)
            matches.append(
                (scope_gap, priority, not exact, -(secret.id or 0), tool, secret, method)
            )
    if not matches:
        if denied:
            raise ResolutionFailed(
                "tool_access_denied", status_code=403,
                detail=f"a {service} authorization exists, but you do not have access to its tool",
            )
        return None
    matches.sort(key=lambda item: item[:4])
    _, _, _, _, tool, secret, method = matches[0]
    return tool, secret, method


def _authorization_error(
    ep: dict, method: str, *, code: str, explanation: str, scopes: list[str], authorization=None,
) -> ResolutionFailed:
    provider = oauth_providers.get(ep["provider"])
    capability = (
        connection_authorization.connect_capability(provider, ep, authorization)
        if provider else str(ep.get("authorization_capability") or "")
    )
    command = f"treg connections connect --provider {ep['provider']}"
    if capability:
        command += f" --capability {capability}"
    return ResolutionFailed("authorization_required", status_code=428, detail={
        "error": code,
        "provider": ep["provider"],
        "endpoint_id": ep["id"],
        "required_authorization_method": method,
        "required_capability": capability,
        "required_scopes": scopes,
        "message": explanation,
        "cli_command": command,
        "dashboard_action": {
            "label": connection_authorization.action_label(authorization, capability),
            "url": "/app#connections",
        },
    })


def _preflight_authorization(ep: dict, secret: Secret, method: str, authorization=None) -> None:
    required = connection_authorization.required_scopes(ep, authorization)
    state = expiry_state(secret.expires_at, oauth.secret_is_refreshable(secret))
    if state == "expired":
        raise _authorization_error(
            ep, method, code="authorization_expired",
            explanation=f"The {method} authorization has expired. A human must authorize it again.",
            scopes=required, authorization=authorization,
        )
    missing = [scope for scope in required if scope not in secret.granted_scopes.split()]
    if missing:
        raise _authorization_error(
            ep, method, code="authorization_scope_required",
            explanation="The connected authorization does not include every permission that this tool requires.",
            scopes=missing, authorization=authorization,
        )
    if ep.get("required_resource") and not secret.resource_ref:
        raise _authorization_error(
            ep, method, code="authorization_resource_required",
            explanation=(
                authorization.missing_message
                if authorization and authorization.missing_message else
                "No usable account is selected for this authorization."
            ),
            scopes=required, authorization=authorization,
        )


async def _resolve_marketplace_call(
    ep: dict, *, method: str, query: QueryValues, has_body: bool,
    read_body: Callable[[], Awaitable[bytes]], caller: Caller, db: AsyncSession,
    resolve_call: Callable[[str, Caller, AsyncSession], Awaitable[ResolvedTarget]],
    authorization_method: str = "",
) -> MarketplaceCall:
    """Resolve a catalog call, selecting an explicit OAuth grant before host matching.

    Endpoints without authorization metadata retain the normal tool → credential → platform
    ladder. Annotated endpoints select by provider plus grant method. That generic identity avoids
    ambiguous same-host tools without teaching the faithful relay about Instagram or Meta.
    """
    await _enforce_capability_pin(ep, caller, db)
    _enforce_catalog_status(ep)
    service = ep["provider"]
    provider = oauth_providers.get(service)
    if provider is None or not provider.base_url:
        raise ResolutionFailed(
            "injection_failed", status_code=502,
            detail=f"{ep['id']} is cataloged but {service!r} isn't proxy-callable yet")
    if method.upper() != (ep.get("method") or "GET").upper():
        raise ResolutionFailed(
            "method_mismatch", status_code=400,
            detail=f"{ep['id']} is {ep['method']} — add --method {ep['method']}")

    try:
        methods = connection_authorization.select_endpoint_methods(
            ep, authorization_method,
        )
    except ValueError as exc:
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400, detail=str(exc),
        ) from None
    chosen_tool: Tool | None = None
    chosen_secret: Secret | None = None
    chosen_method = ""
    if methods:
        authorization = None
        grant = await _provider_tool_grant(service, methods, caller, db, endpoint=ep)
        if grant is not None:
            chosen_tool, chosen_secret, chosen_method = grant
        else:
            chosen_secret = await _marketplace_secret(service, caller.org_id, db, methods)
            if chosen_secret is not None:
                chosen_method = _authorization_method(chosen_secret)
        if chosen_secret is None:
            required_method = methods[0]
            authorization = connection_authorization.method_spec(provider, required_method)
            raise _authorization_error(
                ep, required_method, code="authorization_missing",
                explanation=(
                    authorization.missing_message if authorization and authorization.missing_message
                    else f"This tool requires {provider.display_name} authorization."
                ),
                scopes=connection_authorization.required_scopes(ep, authorization),
                authorization=authorization,
            )
        authorization = connection_authorization.method_spec(provider, chosen_method)
        _preflight_authorization(ep, chosen_secret, chosen_method, authorization)
        provider = provider.profile_for_authorization(chosen_method)

    upstream, consumed = _marketplace_upstream(ep, provider, query, chosen_method)
    body = await read_body() if has_body else b""
    phash = _params_hash(ep["id"], query.multi_items(), body)
    # The catalog's estimate travels on EVERY tier - informational on tiers 1/2 (the provider bills
    # the org's own account; Activity shows "estimated") and the reserve amount on tier 4 only
    # (`metered` gates the ledger, so this never charges a balance for an own-key call).
    cat = catalog_store.load()
    raw_cost = ep.get("cost") or {}
    cv = cat.cost_view(raw_cost, service) if raw_cost else None
    info_est, info_unit = _marketplace_pricing(service, ep["id"], cv, query, body)
    request_data = settlement_basis.request_evidence(
        query.multi_items(), body, path_names=consumed)
    unit_view = cat.cost_view({**raw_cost, "value": 1, "per": 1}, service) if raw_cost else None
    unit_micro = _usd_to_micro(unit_view.get("usd")) if unit_view else 0
    basis = settlement_basis.derive_basis(
        raw_cost, request=request_data, input_schema=ep.get("input") or {},
        unit_micro=unit_micro, terminal=bool(ep.get("async")),
        response_estimate_micro=info_est,
    )
    if basis.get("amount", {}).get("kind") in ("table", "usage"):
        info_est = int(basis["reserve_micro"])
    common = dict(
        upstream=upstream, consumed=consumed, endpoint_id=ep["id"], provider=service,
        params_hash=phash, cost_type=str((ep.get("cost") or {}).get("type") or ""),
        estimate_micro=info_est,
        # The per-ROW price, carried on every tier (settle only reads it on metered calls):
        # a `per_result` settle that can't count rows can only ever bill the estimate,
        # which is how 6,000 delivered Bright Data records once billed as one (2026-08-24).
        unit_micro=info_unit, settlement_basis=basis, request_data=request_data,
        async_descriptor=ep.get("async"), resource_ownership=ep.get("resource_ownership"),
    )
    if chosen_tool is not None:
        return MarketplaceCall(tool=chosen_tool, tier="tool", **common)

    if not methods:
        try:  # tier 1 - the org registered this provider: their tool, their bindings, their ACLs
            target = await resolve_call(upstream, caller, db)
            return MarketplaceCall(
                tool=target.tool, tier="tool", **{**common, "upstream": target.upstream})
        except ResolutionFailed as exc:
            if exc.status_code != 404:  # 403 (ACL) / 409 (ambiguous) are real answers, not fall-through
                raise

    secret = chosen_secret or await _marketplace_secret(service, caller.org_id, db)  # tier 2
    if secret is not None:
        virtual = Tool(
            org_id=caller.org_id, name=ep["id"], owner=secret.owner,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_provider_bindings(provider, secret),
        )
        return MarketplaceCall(tool=virtual, tier="credential", **common)

    # tier 4 — treg's own key, metered against the org's balance. Shadowed by tiers 1 and 2 above:
    # an org that brought its own credential is billed by the provider, not by us, and must never be
    # silently switched onto our key (their quota, their rate limits, their data agreements).
    cost = _platform_offer(ep, provider, caller.org)
    async_owner_call_id = None
    if cost is not None:
        _enforce_platform_pricing_selectors(ep, body)
        async_owner_call_id = await _enforce_platform_async_ownership(ep, query, caller, db)
    skip_direct = False
    probe_lock_id = None
    if cost is not None and capacity_view.is_exhausted(service, ep["id"]):
        # treg's own account for this call is known to be out (the call-path lock, or the sweep).
        # A lock admits one probe a minute so a recovered account is noticed. Otherwise never
        # relay a call we know will 402: with an enabled overflow route the ladder skips straight
        # to the child cycle (plan §4); else refuse BEFORE reserve with a typed 503 naming when
        # and what else (§4.2).
        lock = capacity_view.active_lock(service, ep["id"])
        if lock is not None and capacity_marks.probe_due(lock.key):
            probe_lock_id = lock.lock_id
        elif (get_settings().overflow_mode == "on" and not caller.org.platform_overflow_disabled
                and overflow_routes_view.for_endpoint(ep["id"])):
            skip_direct = True
        else:
            raise _provider_capacity_unavailable(
                ep, service, capacity_view.exhausted_until(service, ep["id"]),
                probing=lock is not None)
    if cost is not None:
        virtual = Tool(
            org_id=caller.org_id, name=ep["id"], owner=caller.email,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_platform_bindings(provider),
        )
        return MarketplaceCall(tool=virtual, tier="platform", skip_direct=skip_direct,
                               async_owner_call_id=async_owner_call_id,
                               probe_lock_id=probe_lock_id, **{
            **common, "cost_type": str(cost.get("type") or "per_call"),
            "estimate_micro": info_est, "unit_micro": info_unit})
    raise _marketplace_no_credential(service, ep["id"], provider, ep)


def _provider_capacity_unavailable(ep: dict, service: str, resets, *,
                                   probing: bool = False) -> ResolutionFailed:
    """The typed floor (plan §4.5): no charge, `resets_at` when known, and the same-capability
    alternatives — treg names them and leaves the choice to the caller (charter: no failover)."""
    lines = [f"treg's own {service} account is out of capacity right now — {ep['id']} can't be "
             f"served on treg's key" + (f" until about {resets:%Y-%m-%d %H:%M} UTC" if resets else "")]
    if probing:
        lines.append("  treg retries the vendor about once a minute and lifts this as soon as it "
                     "answers, so a retry later may succeed")
    lines.append(f"  use your own key: treg secret add {service} --env-var "
                 f"{service.upper().replace('-', '_')}_API_KEY  (own keys are never affected)")
    lines.extend(_capability_alternatives(ep))
    return ResolutionFailed("provider_capacity", status_code=503, detail={
        "error": "provider_capacity_unavailable", "provider": service, "endpoint_id": ep["id"],
        "resets_at": resets.isoformat() + "Z" if resets else None,
        "alternatives": [ln.strip() for ln in _capability_alternatives(ep)[1:]],
        "message": "\n".join(lines),
    })


async def resolve_marketplace_target(
    ep: dict,
    *,
    method: str,
    query: QueryValues,
    has_body: bool,
    read_body: Callable[[], Awaitable[bytes]],
    caller: Caller,
    resolve_call: Callable[[str, Caller, AsyncSession], Awaitable[ResolvedTarget]],
    authorization_method: str = "",
) -> MarketplaceCall:
    # The exhausted view is refreshed here — before the resolution session opens, so at most one
    # connection is held at a time, and before any hold exists. Cached 60 s; a stale or empty view
    # never refuses (plan §4.1: blocking fires on confirmed signals only).
    await capacity_view.load()
    if get_settings().overflow_mode != "off":
        await overflow_routes_view.load()  # same discipline: before the session, cached 60 s
    async with session_maker() as db:
        return await _resolve_marketplace_call(
            ep,
            method=method,
            query=query,
            has_body=has_body,
            read_body=read_body,
            caller=caller,
            db=db,
            resolve_call=resolve_call,
            authorization_method=authorization_method,
        )


def _may_have_body(raw_headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    """Whether this request could carry a body worth hashing. Mirrors proxy._has_body — a GET with no
    content-length must not be awaited for a body it never sends."""
    headers = {name.lower(): value for name, value in raw_headers}
    cl = headers.get(b"content-length", b"").decode("latin-1") or None
    if cl is not None and cl != "0":
        return True
    return "chunked" in headers.get(b"transfer-encoding", b"").decode("latin-1").lower()
