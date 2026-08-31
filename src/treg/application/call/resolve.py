"""Target resolution and marketplace pricing for proxied calls."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import oauth_providers
from ... import sandbox as demo_sandbox
from ...config import get_settings, platform_setting_name
from ...domain.capacity.routes_view import view as overflow_routes_view
from ...domain.capacity.view import view as capacity_view
from ...domain.catalog import store as catalog_store
from ...domain.governance import access as access_policy
from ...domain.identity.access import Caller
from ...infra.db import session_maker
from ...models import CapabilityPin, Org, Secret, Tool
from ..connect import _host_of, _provider_bindings
from .types import ResolutionFailed, ResolvedTarget


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


async def _marketplace_secret(service: str, org_id: int, db: AsyncSession) -> Secret | None:
    """Tier 2's credential: an org secret tagged with this provider (registry connects), else one
    NAMED exactly for it (`treg secret add tikhub …`). Newest wins — a reconnect supersedes."""
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
                 "pageSize", "perPage", "numResults", "maxResults")  # camelCase: companyenrich, exa, lusha


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
    if cost.get("type") in ("per_result", "quota_rows"):
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


def _marketplace_upstream(ep: dict, provider, query_params) -> tuple[str, set[str]]:
    """The full upstream URL for an endpoint-id call, with `{placeholder}` path params filled from
    the caller's query params (they are consumed — dropped from the relayed query). Missing
    required params fail HERE, before a credential is touched or money spent."""
    path, consumed = ep["path"] or "/", set()
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
    inp = ep.get("input") or {}
    required = [k for k, v in (inp.get("queryParams") or {}).items()
                if isinstance(v, dict) and v.get("required") and query_params.get(k) is None]
    if required:
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400, detail=(
                f"{ep['id']} requires --query "
                + " --query ".join(f"{k}=<value>" for k in required)))
    return provider.base_url.rstrip("/") + "/" + path.lstrip("/"), consumed


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


async def _resolve_marketplace_call(
    ep: dict, *, method: str, query: QueryValues, has_body: bool,
    read_body: Callable[[], Awaitable[bytes]], caller: Caller, db: AsyncSession,
    resolve_call: Callable[[str, Caller, AsyncSession], Awaitable[ResolvedTarget]],
) -> MarketplaceCall:
    """Walk the credential ladder for a catalog endpoint id → a `MarketplaceCall`.

    The tool is either the org's own registered tool for that provider (tier 1 — passthrough
    resolution, so ACL filtering and the provider-owned tiebreak apply unchanged) or a virtual,
    never-persisted Tool named after the ENDPOINT (tiers 2 and 4) — so the audit trail records the
    endpoint id, and a member's restricted tool list can never contain it (governance: restricted
    members get no direct marketplace calls; `_require_tool_use` enforces that downstream).

    NOTHING is reserved here. Resolution only PRICES the call; `call_tool` reserves after the deny
    rules and caps have had their say, so a refused call never has to un-hold money."""
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
    upstream, consumed = _marketplace_upstream(ep, provider, query)
    # The telemetry identity of this call, computed once. The body is read here (Starlette caches it,
    # so the relay still streams the same bytes) only for its HASH — never stored, never logged.
    body = await read_body() if has_body else b""
    phash = _params_hash(ep["id"], query.multi_items(), body)
    # The catalog's estimate travels on EVERY tier — informational on tiers 1/2 (the provider bills
    # the org's own account; Activity shows "estimated") and the reserve amount on tier 4 only
    # (`metered` gates the ledger, so this never charges a balance for an own-key call).
    cv = catalog_store.load().cost_view(ep.get("cost"), service) if ep.get("cost") else None
    info_est, info_unit = _marketplace_pricing(
        service, ep["id"], cv, query, body)
    common = dict(upstream=upstream, consumed=consumed, endpoint_id=ep["id"], provider=service,
                  params_hash=phash, cost_type=str((ep.get("cost") or {}).get("type") or ""),
                  estimate_micro=info_est,
                  # The per-ROW price, carried on every tier (settle only reads it on metered calls):
                  # a `per_result` settle that can't count rows can only ever bill the estimate,
                  # which is how 6,000 delivered Bright Data records once billed as one (2026-08-24).
                  unit_micro=info_unit)
    try:  # tier 1 — the org registered this provider: their tool, their bindings, their ACLs
        target = await resolve_call(upstream, caller, db)
        return MarketplaceCall(
            tool=target.tool, tier="tool", **{**common, "upstream": target.upstream})
    except ResolutionFailed as exc:
        if exc.status_code != 404:  # 403 (ACL) / 409 (ambiguous) are real answers, not fall-through
            raise
    secret = await _marketplace_secret(service, caller.org_id, db)  # tier 2 — credential, no tool
    if secret is not None:
        virtual = Tool(  # NEVER added to the session — no registry pollution, by design
            org_id=caller.org_id, name=ep["id"], owner=secret.owner,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_provider_bindings(provider, secret),
        )
        return MarketplaceCall(tool=virtual, tier="credential", **common)
    # tier 4 — treg's own key, metered against the org's balance. Shadowed by tiers 1 and 2 above:
    # an org that brought its own credential is billed by the provider, not by us, and must never be
    # silently switched onto our key (their quota, their rate limits, their data agreements).
    cost = _platform_offer(ep, provider, caller.org)
    skip_direct = False
    if cost is not None and capacity_view.is_exhausted(service):
        # treg's own account for this provider is known to be out (a confirmed balance/quota
        # signature, or the sweep). Never relay a call we know will 402: with an enabled overflow
        # route the ladder skips straight to the child cycle (plan §4); otherwise refuse BEFORE
        # reserve with a typed 503 naming when and what else (§4.2).
        if (get_settings().overflow_mode == "on" and not caller.org.platform_overflow_disabled
                and overflow_routes_view.for_endpoint(ep["id"])):
            skip_direct = True
        else:
            raise _provider_capacity_unavailable(ep, service, capacity_view.get(service))
    if cost is not None:
        virtual = Tool(
            org_id=caller.org_id, name=ep["id"], owner=caller.email,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_platform_bindings(provider),
        )
        return MarketplaceCall(tool=virtual, tier="platform", skip_direct=skip_direct, **{
            **common, "cost_type": str(cost.get("type") or "per_call"),
            "estimate_micro": info_est, "unit_micro": info_unit})
    raise _marketplace_no_credential(service, ep["id"], provider, ep)


def _provider_capacity_unavailable(ep: dict, service: str, state) -> ResolutionFailed:
    """The typed floor (plan §4.5): no charge, `resets_at` when known, and the same-capability
    alternatives — treg names them and leaves the choice to the caller (charter: no failover)."""
    resets = getattr(state, "exhausted_until", None)
    lines = [f"treg's own {service} account is out of capacity right now — {ep['id']} can't be "
             f"served on treg's key" + (f" until about {resets:%Y-%m-%d %H:%M} UTC" if resets else "")]
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
        )


def _may_have_body(raw_headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    """Whether this request could carry a body worth hashing. Mirrors proxy._has_body — a GET with no
    content-length must not be awaited for a body it never sends."""
    headers = {name.lower(): value for name, value in raw_headers}
    cl = headers.get(b"content-length", b"").decode("latin-1") or None
    if cl is not None and cl != "0":
        return True
    return "chunked" in headers.get(b"transfer-encoding", b"").decode("latin-1").lower()
