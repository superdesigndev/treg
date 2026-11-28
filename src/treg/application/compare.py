"""Pairwise comparisons: a second vendor's answer to a recent routed call's input, judged blinded.

Nothing here runs on a call. `treg-worker compare collect` (hourly) reads recent routed calls
(`treg.<capability>` parents) on a listed capability, recovers the contract input and A's answer
(the served child's) from what the archive already holds, chooses B among the capability's other
vendors (the least compared next to A), and asks B once: B's adapter builds B's request from the contract input, and the relay
sends it on treg's key to B's endpoint. No router, no fallback, no overflow, no hold or ledger
entry: treg pays B and logs the catalog price as the estimate. Both answers are read through their
adapters (`route.child_outcome`), so both are contract `output`, and stored
(`domain.feedback.comparisons`); a later call by the team's agent is invited to compare them
(`invite.py`, kind `compare`), and `GET /comparisons/next` and `POST /comparisons` show the pair as
answer 1 and 2 and record the verdict. See docs/context/architecture/feedback.md, "Pairwise
comparisons".
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import delete, exists
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .. import hints, jsontext, oauth_providers
from ..config import get_settings
from ..domain.catalog import store as catalog_store
from ..domain.feedback import comparisons
from ..infra import kv
from ..infra.db import session_maker
from ..infra.upstream.relay import relay
from ..models import (CallComparison, CallRecord, CallReview, CapabilityPin, DenyRule, Membership,
                      Org)
from ..timeutil import utcnow_naive

if TYPE_CHECKING:
    from ..domain.identity.access import Caller
    from .call.types import CallerSnapshot

log = logging.getLogger(__name__)

READY_TTL_S = 3 * 86400
# The `CallReview.client` of a review written on the team MCP: its review tool and a verdict
# riding on the next call (`mcp.PRIOR_CLIENT`). That surface shows a compare invitation.
MCP_ANSWERING_CLIENTS = ("mcp", "mcp-prior")
# A comparison shown inside a call result (team MCP) or printed beside one (CLI): each answer's
# first rows, long strings cut, then a size cap.
INLINE_ROWS = 5
INLINE_FIELD_CHARS = 300
INLINE_ANSWER_CHARS = 3000
# A call's eligibility marker lives this long; a run never looks further back.
MARKER_TTL_S = 2 * 3600
MARKER_TIMEOUT_S = 0.2
MARKER_INPUT_MAX_BYTES = 8192
_ROUTE_HEADER_PREFIX = b"x-treg-route-"
READY_DAYS = 3
INPUT_MAX_BYTES = 2048
# Recent routed calls read per capability per run, newest first: the bound on what a run scans.
SCAN_PER_CAPABILITY = 500
# A B whose last `FAIL_STREAK` answers in `FAIL_WINDOW_H` hours all failed is not asked again until
# the oldest of them leaves the window: a vendor that keeps failing stops taking the day's calls.
FAIL_STREAK = 2
FAIL_WINDOW_H = 24
# Pause between two calls to a vendor: the run is a trickle, never a burst.
CALL_GAP_S = 1.0
CALL_TIMEOUT_S = 60
ANSWER_MAX_BYTES = 8 * 1024 * 1024
VENDOR = "[vendor]"
# Keys that name a vendor or page through one, removed at any depth; `source` only when it names one.
_DROP_KEYS = frozenset({"_treg", "next_cursor", "cursor", "provider", "served_by", "endpoint_id"})

_vendors_cache: tuple[tuple, int, dict[str, frozenset[str]]] | None = None


def ready_key(org_id: int) -> str:
    return f"compare-ready:{org_id}"


def caller_allowed(caller: Caller | CallerSnapshot) -> bool:
    member = caller.membership
    return (caller.org.archive_opt_out_at is None and member.pinned_tags is None
            and member.tool_access is None and member.project_access is None)


def hint_budget_key(org_id: int) -> str:
    return f"compare-hint-budget:{org_id}"


def answering_key(org_id: int) -> str:
    """When the team's agent last answered a review invitation from a client that declared it
    shows compare (`X-Treg-Hints`), an ISO time: a CLI reports only the agent it runs under, so an
    old and a new release look alike in `CallReview.client`. Read against the current window, so
    a shorter `compare_answer_days` takes effect at once."""
    return f"compare-answering:{org_id}"


# How long the note is kept, at least: a window lengthened up to this reaches reviews already noted.
ANSWERING_NOTE_DAYS = 30


async def note_answering(org_id: int) -> None:
    days = max(ANSWERING_NOTE_DAYS, get_settings().compare_answer_days)
    await kv.store().set_text(answering_key(org_id), utcnow_naive().isoformat(), days * 86400)


def _answered_since(value: str | None, since: datetime) -> bool:
    try:
        return value is not None and datetime.fromisoformat(value) >= since
    except ValueError:
        return False


# The marker's format version, in its key: a run reads only markers written by a call path that
# applies its own rules (v2 never marks a merged answer), so one an earlier build wrote is ignored
# rather than compared under rules it never checked.
MARKER_VERSION = "v2"


def marker_key(call_ref: str) -> str:
    return f"compare-ok:{MARKER_VERSION}:{call_ref}"


def configured() -> bool:
    return get_settings().compare_configured


# Why calls on the call path were or were not marked, by reason and client, logged as one line
# every `MARK_LOG_EVERY_S` per process: counts only, never an input or a team.
MARK_LOG_EVERY_S = 600
_mark_counts: dict[str, int] = {}
_mark_logged_at = time.monotonic()


def _note_mark(reason: str, raw_headers) -> None:
    global _mark_logged_at
    client = next((bytes(v).decode("latin-1")[:32] for n, v in raw_headers
                   if bytes(n).lower() == b"x-treg-client"), "") or "-"
    key = f"{reason}/{client}"
    _mark_counts[key] = _mark_counts.get(key, 0) + 1
    now = time.monotonic()
    if now - _mark_logged_at >= MARK_LOG_EVERY_S:
        log.info("compare markers %s", json.dumps(_mark_counts, sort_keys=True))
        _mark_counts.clear()
        _mark_logged_at = now


def _mark_reason(capability: str | None, raw_headers, summary: dict, archive_opted_out: bool,
                 call_ref: str) -> str | None:
    """Why this call gets no marker, or None when it does (checked in the order that decides)."""
    settings = get_settings()
    if not settings.compare_configured:
        return "off"
    if capability not in comparable():
        return "not_listed"
    if archive_opted_out:
        return "archive_off"
    if "cursor" in summary:
        return "page_continuation"
    if summary.get("merged_from"):
        # The caller got several vendors' rows as one answer: no single vendor's answer is A.
        return "merged"
    if not isinstance(summary.get("identity"), dict):
        return "no_input"
    if any(bytes(name).lower().startswith(_ROUTE_HEADER_PREFIX) for name, _ in raw_headers):
        return "route_header"
    if not hints.sampled_at("compare", call_ref, settings.compare_rate):
        return "not_sampled"
    return None


async def mark_eligible(call_ref: str, capability: str | None, raw_headers,
                        route_summary: dict | None = None, *, archive_opted_out: bool = False) -> None:
    """On the call path, after a successful routed answer: mark the call for a comparison run when
    it samples in, its capability is listed (`comparable`), the caller sent no `X-Treg-Route-*` header (a caller
    that chose vendors or routing is left alone) and its team keeps an archive. The marker holds
    the contract input the caller sent (`route_summary["identity"]`, at most
    `MARKER_INPUT_MAX_BYTES`), so the run asks B exactly that question instead of reading it back
    from A's request. A page continuation (`cursor` in the summary) is no fresh question, since B
    would be asked page one, so it is never marked. One short store write, no database, nothing
    raised, nothing about the answer changed. Every outcome is counted (`_note_mark`)."""
    reason = "error"
    try:
        summary = route_summary or {}
        reason = _mark_reason(capability, raw_headers, summary, archive_opted_out, call_ref) or ""
        if reason:
            return
        text = json.dumps(summary["identity"], sort_keys=True, default=str)
        if len(text.encode()) > MARKER_INPUT_MAX_BYTES:
            reason = "input_too_large"
            return
        store = kv.store()
        reason = "store_failed"
        async with asyncio.timeout(MARKER_TIMEOUT_S):
            stored = await store.set_text(marker_key(call_ref), text, MARKER_TTL_S)
        if stored:
            reason = "marked"
    except Exception:  # noqa: BLE001 - a lost marker only means this call is never compared
        return
    finally:
        try:
            _note_mark(reason, raw_headers)
        except Exception:  # noqa: BLE001 - counting never affects the call
            pass


def comparable() -> dict[str, frozenset[str]]:
    """The listed capabilities (`compare_capabilities`) that have a contract and at least two
    providers with a verified adapter, each with those providers: A is whichever of them served,
    B is chosen per call among the others (`_choose_b`). A listed capability short of two is logged
    once per catalog and setting, and skipped."""
    global _vendors_cache
    settings = get_settings()
    raw = tuple(settings.compare_capabilities)
    cat = catalog_store.load()
    if _vendors_cache is not None and _vendors_cache[:2] == (raw, id(cat)):
        return _vendors_cache[2]
    valid: dict[str, frozenset[str]] = {}
    for capability in raw:
        adapted = frozenset(e["provider"] for e in cat.for_capability(capability)
                            if (ad := cat.adapters.get(e["id"])) is not None and ad.verified)
        if capability not in cat.contracts or len(adapted) < 2:
            log.warning("compare capability %s skipped: fewer than two vendors with a verified adapter",
                        capability)
            continue
        valid[capability] = adapted
    _vendors_cache = (raw, id(cat), valid)
    return valid


def _retention_forbidden(endpoint_id: str | None) -> bool:
    entry = catalog_store.load().by_id.get(endpoint_id or "")
    return bool(entry and entry.get("response_retention") == "forbidden")


# Blinding

def _vendor_pattern(providers: list[str], endpoints: list[str]) -> re.Pattern:
    hosts = []
    for provider in providers:
        base = getattr(oauth_providers.get(provider), "base_url", "") or ""
        if base and (host := httpx.URL(base).host):
            hosts.append(host)
    words = sorted({*hosts, *endpoints, *providers}, key=len, reverse=True)
    return re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w-])",
                      re.IGNORECASE)


def _blind(value, pattern: re.Pattern, providers: set[str]):
    """The answer without vendor identifiers: identifier keys removed at any depth, vendor names,
    endpoint ids and API hosts inside text replaced by `[vendor]`, the text kept."""
    if isinstance(value, dict):
        return {k: _blind(v, pattern, providers) for k, v in value.items()
                if k.lower() not in _DROP_KEYS
                and not (k.lower() == "source" and isinstance(v, str) and v.strip().lower() in providers)}
    if isinstance(value, list):
        return [_blind(v, pattern, providers) for v in value]
    if isinstance(value, str):
        return pattern.sub(VENDOR, value)
    return value


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode()
    return (text, False) if len(data) <= limit else (data[:limit].decode(errors="ignore"), True)


# The run

@dataclass(frozen=True)
class Target:
    """B's endpoint and the provider request its adapter builds from the contract input, as the
    router's child call would send it: the adapter's own values, unformatted, and its JSON body."""
    ep: dict
    adapter: Any
    query: dict
    body: bytes
    price_micro: int


def _b_target(contract, identity: dict, provider: str, *, org_id: int | None = None,
              max_price_micro: int | None = None, given: dict | None = None,
              stats: dict | None = None, health: dict | None = None) -> Target | None:
    """B's endpoint for this input when treg's own key can serve it: one the router would ask for
    this input (`plan.candidates_for` and `build_plan`'s cap, declaration, scope and per-provider
    sampling checks; a judge-only `route: false` adapter builds no request), not paused,
    platform-eligible with a key, not exhausted on treg's primary account, retention allowed, a
    computable price within `max_price_micro` and no second host or path parameter to resolve.
    The provider's endpoints are tried in the order the router ranks them (`plan.rank` over the
    caller's keys `given`, the filters each can express, its price and the endpoints' observed
    `stats`), so B answers the question the router would have asked it, and one dear endpoint does
    not hide a cheaper one that answers the same input. `health` is the short-window verdict per
    endpoint (`route_health`): a failing endpoint is never asked (a comparison never takes a
    probe turn), a slow one ranks after the healthy ones, as the router ranks it."""
    from httpx import QueryParams

    from ..domain.capacity.marks import spends_balance
    from ..domain.catalog.routing.contracts import flags_off
    from ..domain.catalog.routing.plan import (Candidate, candidates_for, cost_at, ignored_filters,
                                               miss_charge_micro, rank, sampled_provider_allowed,
                                               unscoped)
    from .call import keyslots, route
    from .call.resolve import _marketplace_pricing, param_check_refuses

    cat = catalog_store.load()
    settings = get_settings()
    registered = oauth_providers.get(provider)
    if (registered is None or not registered.base_url or not registered.uses_pasted_secret
            or oauth_providers.is_paused(provider) or not settings.platform_key_for(provider)
            or not all(settings.platform_credential(b["platform_setting"], b.get("account"))
                       for b in oauth_providers.platform_bindings(registered) if b.get("platform_setting"))):
        return None
    # The router leaves a provider out of a routed request its sampling did not select
    # (`route_provider_sample_pct`); B is never a vendor the router would not have asked.
    if not sampled_provider_allowed(provider, settings.route_provider_sample_percentages, org_id=org_id,
                                    capability=contract.capability, identity=flags_off(contract, identity)):
        return None
    mine = [ep for ep in cat.for_capability(contract.capability) if ep["provider"] == provider]
    fitting = []
    for ep, adapter, variant in candidates_for(contract, mine, cat.adapters, identity)[0]:
        if (ep.get("async") or ep.get("host") or ep.get("platform_auth") == "anonymous"
                or "{" in str(ep.get("path") or "")):
            continue
        if any(route._above(identity.get(k), m) for k, m in (getattr(adapter, "filter_max", None) or {}).items()):
            continue
        if route._declaration_refusal(ep, adapter, identity, variant) and param_check_refuses():
            continue
        if unscoped(adapter, contract, identity):
            continue
        if catalog_store.paused(ep) or _retention_forbidden(ep["id"]) or not cat.platform_eligible(ep):
            continue
        if keyslots.exhausted(provider, ep["id"], None,
                              metered=spends_balance((ep.get("cost") or {}).get("type"))):
            continue
        fitting.append((ep, adapter, variant))
    # As `build_plan` ranks a treg-key child: a `false` flag asks for nothing, so it is not given.
    sent = {k for k, v in (identity if given is None else given).items() if v not in (None, "") and v is not False}
    ranked = []
    for ep, adapter, variant in fitting:
        verdict = (health or {}).get(ep["id"], "ok")
        if verdict == "failing":
            continue
        st = (stats or {}).get(ep["id"]) or {}
        cv = cat.cost_view(ep.get("cost"), provider)
        price = route._table_price(cat, ep, adapter, identity, variant) or cost_at(cv, identity, adapter)
        ignored = ignored_filters(adapter, contract, identity, sent)
        hard = ignored_filters(adapter, contract, identity, sent, soft=False)
        ranked.append(Candidate(
            endpoint=ep, adapter=adapter, variant=variant, tier="platform", price_micro=price,
            hit_rate=st.get("hit_rate"), ok_rate=st.get("ok_rate"), p50_ms=st.get("p50_ms"),
            last_ok_days=st.get("last_ok_days"), p95_ms=st.get("p95_ms"), ignored=ignored,
            hinted=tuple(k for k in ignored if k not in hard),
            miss_micro=miss_charge_micro(cv, identity, adapter, price), health=verdict))
    for c in rank(ranked, given=sent, derive=contract.derive, one_subject=contract.one_subject,
                  last=contract.ask_last):
        ep, adapter, variant = c.endpoint, c.adapter, c.variant
        query, body = adapter.to_upstream(identity, variant)
        payload = route._child_body(ep, body)
        cost = cat.cost_view(ep.get("cost"), provider)
        if not cost or cost.get("usd") is None:
            continue
        price = _marketplace_pricing(provider, ep["id"], cost, QueryParams(query), payload)[0]
        if max_price_micro is not None and price > max_price_micro:
            continue
        return Target(ep=ep, adapter=adapter, query=dict(query), body=payload, price_micro=price)
    return None


async def _ask_b(target: Target, client: httpx.AsyncClient, org_id: int) -> tuple[int, bytes]:
    """One request to B's endpoint on treg's primary key, through the relay alone: (status, body),
    status 0 when no answer came. The request is the one the router's first child call sends for
    the team's question on treg's key (`route._child_input`, then the call path's rewrites:
    treg's `User-Agent`, the provider's client header, its ceiling parameter at the router's
    default ceiling)."""
    from ..infra.upstream.relay import identify_as_treg, tag_shared_client
    from ..models import Tool
    from .call import route
    from .call.resolve import QueryValues, _marketplace_upstream
    from .call.service import ceiling_query
    from .call.types import UpstreamRequest
    from .connect import _host_of

    ep = target.ep
    provider = oauth_providers.get(ep["provider"])
    items = tuple(target.query.items())
    upstream, consumed = _marketplace_upstream(ep, provider, QueryValues(items))
    tool = Tool(org_id=org_id, name=ep["id"], owner="compare", base_url=provider.base_url,
                host=_host_of(provider.base_url), bindings=oauth_providers.platform_bindings(provider))
    headers = tag_shared_client(identify_as_treg(()),
                                catalog_store.load().client_headers.get(ep["provider"]),
                                ep["provider"], org_id)
    if target.body:
        headers += ((b"content-type", b"application/json"),
                    (b"content-length", str(len(target.body)).encode()))
    items += ceiling_query(ep["id"], ep["provider"], route.DEFAULT_MAX_COST_MICRO, items)

    async def body_stream():
        if target.body:
            yield target.body

    async def body_read() -> bytes:
        return target.body

    # `body_read`, as the call path offers it: a provider whose key goes in the JSON body
    # (`location: json`) is injected by reading the body whole.
    request = UpstreamRequest(method=ep["method"], raw_headers=headers, query_items=items,
                              body_stream=body_stream, has_body=bool(target.body),
                              body_read=body_read if target.body else None)
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            response = await relay(request, upstream, tool, {}, client,
                                   drop_params=consumed or None, force_identity=True)
            try:
                chunks, size = [], 0
                async for chunk in response.body_stream:
                    size += len(chunk)
                    if size > ANSWER_MAX_BYTES:
                        return response.status, b""
                    chunks.append(chunk)
            finally:
                await response.close()
    except (httpx.HTTPError, TimeoutError):
        return 0, b""
    return response.status, b"".join(chunks)


def _judged(ep: dict, contract, adapter, status: int, raw: bytes, identity: dict):
    """One vendor's answer as the router judges a child on treg's key (`route.child_outcome`,
    with the plan's canonical identity)."""
    from .call import route

    return route.child_outcome(ep, contract, adapter, status, raw, tier="platform", identity=identity)


def _output(ep: dict, contract, adapter, status: int, raw: bytes, identity: dict):
    """The contract output of one vendor's answer, read as the router reads a child; None unless a hit."""
    outcome = _judged(ep, contract, adapter, status, raw, identity)
    return outcome.core if outcome.kind == "hit" and outcome.core else None


@dataclass
class Candidate:
    record: CallRecord
    capability: str


async def _recent(session_factory, since: datetime, org_ids: set[int]) -> list[Candidate]:
    """Recent successful routed parents of the comparable capabilities from `org_ids` only, newest
    first, at most `SCAN_PER_CAPABILITY` each: one ordered range of (endpoint_id, created_at) per
    capability. Reading only eligible teams matters: other teams' calls can fill a newest-N
    window read over every team, leaving eligible teams' earlier marked calls unread."""
    cat = catalog_store.load()
    out: list[Candidate] = []
    if not org_ids:
        return out
    async with session_factory() as db:
        for capability in comparable():
            parent = f"treg.{capability}"
            if parent not in cat.by_id:
                continue
            # Only a parent that names what its caller got can be compared (`_served`): filtered
            # before the limit, so calls without that link never take the window.
            rows = (await db.execute(select(CallRecord).where(
                CallRecord.endpoint_id == parent, CallRecord.created_at >= since,
                CallRecord.org_id.in_(org_ids), CallRecord.served_call_ref.is_not(None),
                CallRecord.served_content_hash.is_not(None),
            ).order_by(CallRecord.created_at.desc()).limit(SCAN_PER_CAPABILITY))).scalars().all()
            out += [Candidate(r, capability) for r in rows
                    if r.org_id is not None and r.credential_tier == "routed" and r.call_ref
                    and 200 <= (r.status_code or 0) < 300]
    return out


async def _answering_teams(session_factory, since: datetime) -> set[int]:
    """Teams whose agent answered a review invitation since `since` on a surface that shows a
    compare invitation: the team MCP (`MCP_ANSWERING_CLIENTS`), or any client that declared
    `compare` when it answered (`answering_key`). The only teams a comparison can run for
    (`_eligible_teams`), read before any call so the scan covers their calls alone."""
    async with session_factory() as db:
        rows = (await db.execute(select(CallReview.org_id, CallReview.client).where(
            CallReview.created_at >= since, CallReview.invited.is_(True),
        ).distinct())).all()
    teams = {org for org, client in rows if client in MCP_ANSWERING_CLIENTS}
    others = sorted({org for org, _ in rows} - teams)
    # One round trip for every other team (an unreadable store reads as none of them).
    noted = await kv.store().get_texts([answering_key(org) for org in others])
    return teams | {org for org, value in zip(others, noted) if _answered_since(value, since)}


async def _eligible_teams(session_factory, org_ids: set[int], capabilities: set[str]) -> set[int]:
    """Of `org_ids` (for a run, `_answering_teams`: a team whose agent never answers an invitation
    on a surface that shows compare, such as a pipeline that calls without reading hints, would
    leave its pairs unjudged and take the whole budget), the teams a comparison may run for: in
    the archive, not a demo, and holding no access policy at all (no
    member or project tool ACL, no pin to a tag, no deny rule, no capability pin on a compared
    capability), so B's call never needs the team's authorization emulated."""
    if not org_ids:
        return set()
    async with session_factory() as db:
        teams = set((await db.execute(select(Org.id).where(
            Org.id.in_(org_ids), Org.archive_opt_out_at.is_(None), Org.demo.is_(False),
            Org.public_demo.is_(False)))).scalars())
        if not teams:
            return set()
        # JSON columns: an unset value may read back as JSON null, so they are judged here.
        policy = {org for org, *scopes in (await db.execute(select(
            Membership.org_id, Membership.tool_access, Membership.project_access, Membership.pinned_tags,
        ).where(Membership.org_id.in_(teams)))).all() if any(v is not None for v in scopes)}
        policy |= set((await db.execute(select(DenyRule.org_id).where(
            DenyRule.org_id.in_(teams)).distinct())).scalars())
        policy |= set((await db.execute(select(CapabilityPin.org_id).where(
            CapabilityPin.org_id.in_(teams), CapabilityPin.capability.in_(capabilities)).distinct())).scalars())
    return teams - policy


def _usable_child(child: CallRecord | None) -> CallRecord | None:
    """An archive-served (cached) answer is stale next to a live B, so such a parent is not
    compared. The child the parent names is the one whose answer was delivered, whatever status
    its own row carries (an async submission's row holds the ticket's)."""
    if child is None or child.cached:
        return None
    return child


async def _served_children(session_factory, parents: list[CallRecord]) -> dict[str, CallRecord | None]:
    """For each parent that names the child its caller got (`served_call_ref`), by parent
    `call_ref`: that child, in one read through `ix_callrecord_call_ref`, or None when the row is
    gone or cached. Such a parent is compared on that child alone, never on another attempt. A
    parent naming none is absent: what its caller got cannot be told (`served_call_ref` is NULL
    on an answer the call path could not check, such as one over `route.SERVED_CHECK_MAX`), so it
    is never compared."""
    refs = {p.served_call_ref: p for p in parents if p.served_call_ref}
    out: dict[str, CallRecord | None] = {p.call_ref: None for p in refs.values()}
    if not refs:
        return out
    async with session_factory() as db:
        rows = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref.in_(list(refs)),
            CallRecord.org_id.in_({p.org_id for p in refs.values()})))).scalars().all()
    for row in rows:
        parent = refs.get(row.call_ref)
        if parent is not None and row.org_id == parent.org_id:
            out[parent.call_ref] = _usable_child(row)
    return out


async def _still_eligible(session_factory, org_id: int) -> bool:
    """Right before B is asked: the team still exists, is not suspended or paused by flow control,
    and still has no access policy and no archive opt-out. Any doubt skips the call."""
    from ..infra import org_flow_control

    async with session_factory() as db:
        org = await db.get(Org, org_id)
        if org is None or org.suspended:
            return False
    if not await org_flow_control.confirmed_unpaused(org_id):
        return False
    return org_id in await _eligible_teams(session_factory, {org_id}, set(comparable()))


async def _claim(session_factory, values: dict, limit: int, *, spend_limit_micro: int, team_cap: int,
                 since, ready_since) -> str:
    """In one short transaction, one attempt and B's price on the current UTC day's counts (taken
    first, in one conditional update: on Postgres its row lock orders concurrent claims), check
    archive opt-out and the pair cap, then insert the `pending` row. The team read locks out an
    opt-out update until the claim commits so erasure can find the row.
    `claimed`, `taken`, `gone`, `opted_out`, `capped`, `over_spend` or `at_limit`."""
    async with session_factory() as db:
        if await comparisons.by_call_ref(db, values["call_ref"]) is not None:
            return "taken"
        taken = await comparisons.take_attempt(db, _day(), limit, price_micro=values.get("b_cost_micro", 0),
                                               spend_limit_micro=spend_limit_micro)
        if taken != "taken":
            await db.rollback()
            return taken
        org = (await db.execute(select(Org.archive_opt_out_at).where(
            Org.id == values["org_id"],
        ).with_for_update(read=True))).first()
        if org is None or org.archive_opt_out_at is not None:
            await db.rollback()
            return "gone" if org is None else "opted_out"
        if await comparisons.counted(db, values["org_id"], values["capability"],
                                     (values["a_provider"], values["b_provider"]),
                                     since=since, ready_since=ready_since) >= team_cap:
            await db.rollback()
            return "capped"
        comparisons.add(db, status=comparisons.PENDING, **values)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return "taken"
    return "claimed"


async def _ready_to_dispatch(session_factory, org_id: int, call_ref: str) -> bool:
    """Final check immediately before asking B: the team is still active and opted into the
    archive, and the claimed pending row has not been erased. Removes a still-pending row when
    the team opted out or was suspended/deleted after claim."""
    async with session_factory() as db:
        org = await db.get(Org, org_id)
        row = await comparisons.by_call_ref(db, call_ref)
        if row is None or row.status != comparisons.PENDING:
            return False
        if org is None or org.suspended or org.archive_opt_out_at is not None:
            await db.execute(delete(CallComparison).where(
                CallComparison.call_ref == call_ref,
                CallComparison.status == comparisons.PENDING,
            ))
            await db.commit()
            return False
    return True


async def _finish(session_factory, org_id: int, call_ref: str, values: dict) -> bool:
    """Settle the claimed row; drop it if the team was deleted or opted out of the archive
    meanwhile (team deletion and archive erasure remove rows they find, and the next run
    removes any orphan)."""
    async with session_factory() as db:
        org = (await db.execute(select(Org.archive_opt_out_at).where(
            Org.id == org_id,
        ).with_for_update(read=True))).first()
        if org is None or org.archive_opt_out_at is not None:
            await db.execute(delete(CallComparison).where(
                CallComparison.call_ref == call_ref,
                CallComparison.status == comparisons.PENDING,
            ))
            await db.commit()
            return False
        settled = await comparisons.finish(db, call_ref, **values)
        await db.commit()
    if not settled:
        return False
    async with session_factory() as db:
        org = await db.get(Org, org_id)
        if org is None or org.archive_opt_out_at is not None:
            await db.execute(delete(CallComparison).where(CallComparison.call_ref == call_ref))
            await db.commit()
            return False
    return True


def _day() -> str:
    return f"{utcnow_naive():%Y%m%d}"


def _spend_limit_micro() -> int:
    """`compare_daily_usd` in micro-USD, read as the decimal it was written as (2.01 is 2,010,000)."""
    return int(Decimal(str(get_settings().compare_daily_usd)) * 1_000_000)


async def _remove_orphans(session_factory) -> int:
    """Comparisons whose team is gone: team deletion removes them, this catches any it missed."""
    async with session_factory() as db:
        gone = await db.execute(delete(CallComparison).where(
            ~exists().where(Org.id == CallComparison.org_id)))
        await db.commit()
    return gone.rowcount or 0


@dataclass
class Served:
    """A marked routed call whose served child can be compared: everything a run knows before it
    reads an archive body or chooses B."""
    cand: Candidate
    child: CallRecord
    a_ep: dict
    given: dict


def _cell(tally: dict, capability: str, a: str) -> int:
    """Comparisons in the window whose A was `a` on this capability: the run serves the least
    compared served vendor first, so a vendor that rarely serves is still compared as A."""
    return sum(n for (c, x, _), n in tally.items() if c == capability and x == a)


def _pair_count(tally: dict, capability: str, a: str, b: str) -> int:
    return tally.get((capability, a, b), 0) + tally.get((capability, b, a), 0)


async def collect(*, max_seconds: float = 110.0, session_factory=session_maker,
                  client: httpx.AsyncClient | None = None) -> dict:
    """One run: at most `compare_max_per_run` second-vendor calls, never past `compare_max_per_day`
    attempts or `compare_daily_usd` of B's catalog prices for the UTC day. Sequential, `CALL_GAP_S`
    apart. The marked calls are taken least compared served vendor first (`_cell`), and B is the
    capability's least compared vendor next to A (`_choose_b`), so the run spreads its calls
    instead of spending them on whichever capability it read first. Removes orphaned rows first,
    on every run, the feature on or off."""
    from ..domain.capacity.view import view as capacity_view
    from ..domain.catalog.routing.contracts import canonical_identity

    # The budget covers the whole run, choosing the teams and reading their calls included.
    deadline = time.monotonic() + max_seconds
    totals = {"read": 0, "eligible": 0, "marked": 0, "called": 0, "ready": 0, "failed": 0,
              "skipped": 0, "removed": await _remove_orphans(session_factory), "estimate_micro": 0,
              "skip_reasons": {}, "no_marker_by_client": {}, "called_by": {}}
    settings = get_settings()
    if not configured() or settings.compare_rate <= 0:
        return {"status": "off", **totals}
    async with session_factory() as db:
        if (await comparisons.attempts(db, _day()) >= settings.compare_max_per_day
                or await comparisons.spent(db, _day()) >= _spend_limit_micro()):
            return {"status": "at_limit", **totals}
    now = utcnow_naive()
    lookback = timedelta(hours=min(settings.compare_lookback_hours, MARKER_TTL_S // 3600))
    answered = now - timedelta(days=settings.compare_answer_days)
    teams = await _eligible_teams(session_factory, await _answering_teams(session_factory, answered),
                                  set(comparable()))
    found = await _recent(session_factory, now - lookback, teams)
    totals["read"] = len(found)
    async with session_factory() as db:
        done = set((await db.execute(select(CallComparison.call_ref).where(
            CallComparison.call_ref.in_([c.record.call_ref for c in found])))).scalars()) if found else set()
        tally = await comparisons.tallies(db, list(comparable()),
                                          now - timedelta(days=settings.compare_window_days))
        failing = await comparisons.failure_streaks(db, list(comparable()),
                                                    now - timedelta(hours=FAIL_WINDOW_H))
    found = [c for c in found if c.record.call_ref not in done]
    totals["eligible"] = len(found)

    def skip(cand: Candidate | None, reason: str) -> None:
        totals["skipped"] += 1
        reasons = totals["skip_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1
        if reason == "no_marker" and cand is not None:  # which surface the unmarked calls came through
            by_client = totals["no_marker_by_client"]
            name = cand.record.client or "-"
            by_client[name] = by_client.get(name, 0) + 1

    # The call path wrote a marker for each sampled call (no `X-Treg-Route-*` header, a listed
    # capability), holding its input: one round trip for all of them. None, or a store that cannot
    # say, is no comparison.
    store = kv.store()
    markers = await store.get_texts([marker_key(c.record.call_ref) for c in found]) if found else []
    marked: list[tuple[Candidate, str]] = []
    for cand, marker in zip(found, markers):
        if marker is None:
            skip(cand, "no_marker")
        else:
            marked.append((cand, marker))
    # The children the parents name, in one read; no lookup one parent at a time.
    children = await _served_children(session_factory, [c.record for c, _ in marked])
    served: list[Served] = []
    for cand, marker in marked:
        ref = cand.record.call_ref
        if ref not in children:
            skip(cand, "a_delivery_unknown")
            continue
        if children[ref] is None:
            skip(cand, "served_child_missing")
            continue
        try:
            got = _served(cand, marker, children[ref])
        except Exception:
            log.warning("comparison for %s failed", cand.record.call_ref, exc_info=True)
            got = "error"
        if isinstance(got, str):
            skip(cand, got)
        else:
            served.append(got)
    totals["marked"] = len(served)
    cat = catalog_store.load()
    await capacity_view.load()
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=CALL_TIMEOUT_S)
    try:
        while served:
            if totals["called"] >= settings.compare_max_per_run or time.monotonic() >= deadline:
                break
            # The least compared served vendor of any capability first; ties at random.
            pick = min(served, key=lambda s: (_cell(tally, s.cand.capability, s.a_ep["provider"]),
                                              secrets.randbelow(1 << 30)))
            served.remove(pick)
            try:
                outcome = await _one(pick, cat, now, session_factory, client, store, totals, tally,
                                     failing, canonical_identity)
            except Exception:
                log.warning("comparison for %s failed", pick.cand.record.call_ref, exc_info=True)
                outcome = "skipped:error"
            if outcome == "at_limit":
                break
            outcome, _, reason = outcome.partition(":")
            if outcome == "skipped":
                skip(pick.cand, reason)
            else:
                totals[outcome] += 1
    finally:
        if own_client:
            await client.aclose()
    return {"status": "ran", **totals}


def _served(cand: Candidate, marker: str | None, child: CallRecord) -> Served | str:
    """A marked call whose served child (the one its parent names, `_served_children`) can be
    compared, or the reason it cannot: no archive body is read and no vendor chosen yet."""
    if marker is None:
        return "no_marker"
    try:
        given = json.loads(marker)
    except ValueError:
        given = None
    if not isinstance(given, dict):
        return "marker_without_input"
    a_ep = catalog_store.load().by_id.get(child.endpoint_id or "")
    if a_ep is None or a_ep["provider"] not in comparable().get(cand.capability, ()):
        return "a_not_comparable"
    if child.credential_tier != "platform":
        return "a_not_on_treg_key"
    if not cand.record.served_content_hash:
        return "a_delivery_unknown"
    if _retention_forbidden(child.endpoint_id):
        return "a_retention_forbidden"
    return Served(cand=cand, child=child, a_ep=a_ep, given=given)


async def _a_answer(served: Served, session_factory) -> tuple[Any, str | None]:
    """(A's answer as JSON, None), or (None, the reason there is none): the reason is one of a
    fixed set, never anything read from the answer, since a JSON string is an answer too. Only the
    bytes the caller got count: the child's own archived answer (`archive.answer_bytes`, which
    checks the bytes read against the hash) or, for an async provider, the terminal answer filed
    under the child's ref, whichever hashes to the parent's `served_content_hash`."""
    from .. import archive

    child, delivered = served.child, served.cand.record.served_content_hash
    raw = None
    if child.archive_key_hash and child.archive_content_hash == delivered:
        raw = await archive.answer_bytes(child.archive_key_hash, delivered)
    else:
        terminal = (await archive.load_terminal_snapshots(
            [(child.call_ref, child.endpoint_id)], session_factory=session_factory)).get(child.call_ref)
        if terminal is not None and archive.content_hash(terminal[1]) == delivered:
            raw = terminal[1]
    if raw is None:
        return None, "a_answer_not_delivered"
    try:
        return json.loads(raw), None
    except ValueError:
        return None, "a_answer_unreadable"


async def _observed_stats(endpoint_ids: list[str], session_factory=session_maker) -> dict:
    """The endpoints' observed routing stats, as the router ranks with them. A web process reads
    its bound cache (`route._observed_stats`); the worker has none bound, so it reads the same
    day buckets straight from Postgres through the run's own pool. Unavailable stats read as none."""
    from .call import route

    if route._endpoint_observation_reader is not None:
        return await route._observed_stats(endpoint_ids)
    from ..infra.catalog_observations import EndpointObservationUnavailable, PostgresEndpointObservationReader

    try:
        return await PostgresEndpointObservationReader(session_factory).get_many(endpoint_ids)
    except EndpointObservationUnavailable as exc:
        # One endpoint's fold behind keeps the others', as the web cache keeps them.
        log.warning("endpoint stats fold unavailable for comparisons")
        return exc.completed or {}
    except Exception:  # stats only order the endpoints
        log.warning("endpoint stats unavailable for comparisons", exc_info=True)
        return {}


async def _health(endpoint_ids: list[str]) -> dict[str, str]:
    """Each endpoint's short-window verdict (`route_health`) when health guarding is `on`, as the
    router applies it to a treg-key child; nothing in `observe` or `off`."""
    from ..domain.catalog.routing import health as health_rules
    from .call import route_health

    if route_health.mode() != "on" or not endpoint_ids:
        return {}
    tallies = await route_health.read(endpoint_ids)
    limits = route_health.thresholds()
    return {eid: v for eid, tally in tallies.items()
            if (v := health_rules.verdict(tally, limits)) != health_rules.OK}


async def _choose_b(served: Served, contract, identity: dict, tally: dict, failing: dict,
                    session_factory, now) -> tuple[str, Target] | str:
    """B for this call: of the capability's other vendors not excluded (`compare_exclude`) and not
    failing (`FAIL_STREAK`), the least compared next to A across every team (ties at random) with
    an endpoint treg's key can serve for this input (`_b_target`) within `compare_max_b_micro` and
    what is left of the day's spend, and whose pair this team has not filled (`compare_team_cap`).
    Or the reason there is none."""
    settings = get_settings()
    capability, a = served.cand.capability, served.a_ep["provider"]
    org_id = served.cand.record.org_id
    others = [p for p in comparable().get(capability, ()) if p != a and p not in settings.compare_exclude]
    if not others:
        return "b_unavailable"
    if not (others := [p for p in others if failing.get((capability, p), 0) < FAIL_STREAK]):
        return "b_failing"
    others.sort(key=lambda p: (_pair_count(tally, capability, a, p), secrets.randbelow(1 << 30)))
    async with session_factory() as db:
        # Read again at the claim; here it only keeps a B the day cannot pay for from blocking a
        # cheaper one.
        remaining = _spend_limit_micro() - await comparisons.spent(db, _day())
    cat = catalog_store.load()
    ids = [e["id"] for e in cat.for_capability(capability) if e["provider"] in others]
    stats, health = await _observed_stats(ids, session_factory), await _health(ids)
    reason = "b_unavailable"
    for provider in others:
        target = _b_target(contract, identity, provider, org_id=org_id, given=served.given, stats=stats,
                           health=health, max_price_micro=min(settings.compare_max_b_micro, remaining))
        if target is None:
            # Only to say why: the provider's first servable endpoint at any price.
            dear = _b_target(contract, identity, provider, org_id=org_id, given=served.given, stats=stats,
                             health=health)
            if dear is not None:
                reason = "b_too_dear" if dear.price_micro > settings.compare_max_b_micro else "day_spend_cap"
            continue
        async with session_factory() as db:
            counted = await comparisons.counted(
                db, served.cand.record.org_id, capability, (a, provider),
                since=now - timedelta(days=settings.compare_window_days),
                ready_since=now - timedelta(days=READY_DAYS))
        if counted >= settings.compare_team_cap:
            reason = "team_pair_cap"
            continue
        return provider, target
    return reason


async def _one(served: Served, cat, now, session_factory, client, store, totals, tally, failing,
               canonical_identity) -> str:
    """One served call, to `ready`, `failed`, `skipped:<reason>` or `at_limit`. The reasons are
    counted in the run's output (`skip_reasons`), so an empty run says where candidates stopped."""
    settings = get_settings()
    cand, child, a_ep = served.cand, served.child, served.a_ep
    parent = cand.record
    contract, a_adapter = cat.contracts.get(cand.capability), cat.adapters.get(child.endpoint_id)
    if contract is None or a_adapter is None:
        return "skipped:no_contract_or_adapter"
    # The question the caller asked, from the marker: no reading it back from A's request.
    identity, variant = canonical_identity(contract, dict(served.given))
    if variant is None:
        return "skipped:input_not_recovered"
    chosen = await _choose_b(served, contract, identity, tally, failing, session_factory, now)
    if isinstance(chosen, str):
        return f"skipped:{chosen}"
    b_provider, target = chosen
    answer, missing = await _a_answer(served, session_factory)
    if missing is not None:
        return f"skipped:{missing}"
    a_output = _output(a_ep, contract, a_adapter, 200, json.dumps(answer).encode(), identity)
    if a_output is None:
        return "skipped:a_output_unreadable"
    # Wait before the final eligibility check and claim so opt-out during pacing never claims or dispatches.
    if totals["called"]:
        await asyncio.sleep(CALL_GAP_S)
    if not await _still_eligible(session_factory, parent.org_id):
        return "skipped:team_no_longer_eligible"
    claimed = await _claim(session_factory, dict(
        org_id=parent.org_id, user_email=parent.user_email, call_ref=parent.call_ref,
        capability=cand.capability, a_endpoint=child.endpoint_id, a_provider=a_ep["provider"],
        b_endpoint=target.ep["id"], b_provider=b_provider, b_cost_micro=target.price_micro),
        settings.compare_max_per_day, spend_limit_micro=_spend_limit_micro(),
        team_cap=settings.compare_team_cap,
        since=now - timedelta(days=settings.compare_window_days),
        ready_since=now - timedelta(days=READY_DAYS))
    if claimed == "at_limit":
        return "at_limit"
    if claimed == "over_spend":  # a cheaper B may still fit the day
        return "skipped:day_spend_cap"
    if claimed != "claimed":
        return f"skipped:claim_{claimed}"
    # No DB lock spans outbound HTTP (non-negotiable 3); recheck right before dispatch, and let
    # `_finish` discard any in-flight answer whose team opted out or left before settlement.
    if not await _ready_to_dispatch(session_factory, parent.org_id, parent.call_ref):
        return "skipped:opted_out_before_dispatch"
    key = (cand.capability, a_ep["provider"], b_provider)
    tally[key] = tally.get(key, 0) + 1
    by = totals["called_by"]
    by["/".join(key)] = by.get("/".join(key), 0) + 1
    totals["called"] += 1
    totals["estimate_micro"] += target.price_micro
    try:
        status, raw = await _ask_b(target, client, parent.org_id)
    except Exception:
        log.warning("compare call %s for %s failed", target.ep["id"], parent.call_ref, exc_info=True)
        status, raw = 0, b""
    judged = _judged(target.ep, contract, target.adapter, status, raw, identity) if status else None
    log.info("compare call %s for %s: status %s, read as %s%s, catalog price %s micro-USD",
             target.ep["id"], parent.call_ref, status, judged.kind if judged else "no answer",
             f" ({', '.join(judged.why)})" if judged and judged.why else "", target.price_micro)
    b_output = judged.core if judged is not None and judged.kind == "hit" and judged.core else None
    fail_key = (cand.capability, b_provider)
    if b_output is None:
        values = {"status": comparisons.FAILED}
        failing[fail_key] = failing.get(fail_key, 0) + 1
    else:
        providers = [a_ep["provider"], b_provider]
        pattern = _vendor_pattern(providers, [child.endpoint_id, target.ep["id"]])
        limit = settings.compare_max_answer_bytes
        a_text, a_cut = _truncate(jsontext.dumps(_blind(a_output, pattern, set(providers))), limit)
        b_text, b_cut = _truncate(jsontext.dumps(_blind(b_output, pattern, set(providers))), limit)
        input_text, input_cut = _truncate(
            jsontext.dumps(_blind(identity, pattern, set(providers))), INPUT_MAX_BYTES)
        failing[fail_key] = 0
        values = dict(status=comparisons.READY, first=secrets.choice(("a", "b")), input=input_text,
                      a_body=a_text, b_body=b_text,
                      truncated=",".join(p for p, cut in (("input", input_cut), ("a", a_cut),
                                                          ("b", b_cut)) if cut))
    if not await _finish(session_factory, parent.org_id, parent.call_ref, values):
        return "skipped:team_deleted"
    if b_output is None:
        return "failed"
    await store.set_flag(ready_key(parent.org_id), READY_TTL_S)
    return "ready"


def dumps(result: dict) -> str:
    return json.dumps(result, sort_keys=True)


# The team's side: the next pair to judge, and its verdict.

class ComparisonNotFound(Exception):
    pass


def _since():
    return utcnow_naive() - timedelta(days=READY_DAYS)


async def _flag_after_none(org_id: int) -> None:
    """Clear the team's ready flag after finding no waiting pair, then look again: a run that
    stored one in between keeps its invitation."""
    store = kv.store()
    await store.clear_flag(ready_key(org_id))
    async with session_maker() as db:
        row = await comparisons.next_ready(db, org_id, _since())
    if row is not None:
        await store.set_flag(ready_key(org_id), READY_TTL_S)


def _shown_cut(row) -> list[str]:
    cut = set(filter(None, (row.truncated or "").split(",")))
    first, second = ("a", "b") if row.first == "a" else ("b", "a")
    return [name for name, part in (("input", "input"), ("answer_1", first), ("answer_2", second))
            if part in cut]


def compact_answer(text: str | None) -> tuple[str, bool]:
    """(one answer as an invitation shows it, whether anything was left out): its first rows, long
    strings cut, then a size cap. Lists anywhere in the answer keep their first `INLINE_ROWS`."""
    if not text:
        return "", False
    try:
        value = json.loads(text)
    except ValueError:
        return text[:INLINE_ANSWER_CHARS], len(text) > INLINE_ANSWER_CHARS
    cut_any = False

    def cut(v: Any) -> Any:
        nonlocal cut_any
        if isinstance(v, list):
            cut_any |= len(v) > INLINE_ROWS
            return [cut(x) for x in v[:INLINE_ROWS]]
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        if isinstance(v, str) and len(v) > INLINE_FIELD_CHARS:
            cut_any = True
            return v[:INLINE_FIELD_CHARS] + "…"
        return v

    shown = json.dumps(cut(value), ensure_ascii=False, separators=(",", ":"))
    return shown[:INLINE_ANSWER_CHARS], cut_any or len(shown) > INLINE_ANSWER_CHARS


def compact_pair(pair: dict) -> dict:
    """A waiting pair (`next_for`) compacted for an invitation. Cut when stored (`truncated_parts`),
    and rows or text left out here (`shortened`): the agent judges what is shown, and knows when
    the input itself is incomplete."""
    one, one_cut = compact_answer(pair.get("answer_1"))
    two, two_cut = compact_answer(pair.get("answer_2"))
    out = {"comparison_id": pair["comparison_id"], "capability": pair.get("capability"),
           "input": pair.get("input"), "answer_1": one, "answer_2": two}
    if pair.get("truncated"):
        out["truncated_parts"] = pair.get("truncated_parts") or []
    if shortened := [name for name, cut in (("answer_1", one_cut), ("answer_2", two_cut)) if cut]:
        out["shortened"] = shortened
    return out


async def next_for(caller: Caller) -> dict | None:
    """The team's oldest comparison waiting for a verdict, blinded, if the caller may see it."""
    if not caller_allowed(caller):
        return None
    org_id = caller.org_id
    async with session_maker() as db:
        row = await comparisons.next_ready(db, org_id, _since())
    if row is None:
        await _flag_after_none(org_id)
        return None
    one, two = comparisons.shown(row)
    cut = _shown_cut(row)
    return {"comparison_id": row.id, "capability": row.capability, "input": row.input,
            "answer_1": one, "answer_2": two, "truncated": bool(cut),
            **({"truncated_parts": cut} if cut else {})}


async def submit(caller: Caller, *, comparison_id: int, choice: str,
                 reason: str | None, client: str) -> tuple[str, bool]:
    """Record a verdict; returns (the verdict as shown, inserted). A second verdict keeps the first."""
    if not caller_allowed(caller):
        raise ComparisonNotFound
    org_id = caller.org_id
    async with session_maker() as db:
        row = await comparisons.get(db, comparison_id, org_id)
        if row is None or row.status not in (comparisons.READY, comparisons.DECIDED):
            raise ComparisonNotFound
        inserted = await comparisons.decide(db, row, choice=choice, reason=reason, client=client,
                                            at=utcnow_naive())
        await db.commit()
        await db.refresh(row)
        remaining = await comparisons.next_ready(db, org_id, _since())
    if remaining is None:
        await _flag_after_none(org_id)
    return comparisons.shown_choice(row) or choice, inserted
