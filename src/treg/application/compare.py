"""Pairwise comparisons: a second vendor's answer to a recent routed call's input, judged blinded.

Nothing here runs on a call. `treg-worker compare collect` (hourly) reads recent routed calls
(`treg.<capability>` parents) whose served child is one side of a configured pair, recovers the
contract input and A's answer from what the archive already holds for that child, and asks the
pair's other vendor B once: B's adapter builds B's request from the contract input, and the relay
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
from typing import Any

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
# Pause between two calls to a vendor: the run is a trickle, never a burst.
CALL_GAP_S = 1.0
CALL_TIMEOUT_S = 60
ANSWER_MAX_BYTES = 8 * 1024 * 1024
VENDOR = "[vendor]"
# Keys that name a vendor or page through one, removed at any depth; `source` only when it names one.
_DROP_KEYS = frozenset({"_treg", "next_cursor", "cursor", "provider", "served_by", "endpoint_id"})

_pairs_cache: tuple[str, int, dict[str, list[tuple[str, str]]]] | None = None


def ready_key(org_id: int) -> str:
    return f"compare-ready:{org_id}"


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


def marker_key(call_ref: str) -> str:
    return f"compare-ok:{call_ref}"


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
    if capability not in pairs():
        return "not_paired"
    if archive_opted_out:
        return "archive_off"
    if "cursor" in summary:
        return "page_continuation"
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
    it samples in, its capability is paired, the caller sent no `X-Treg-Route-*` header (a caller
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


def pairs() -> dict[str, list[tuple[str, str]]]:
    """The configured pairs whose two providers both have a verified adapter for the capability;
    any other pair is logged once per catalog and setting, and skipped."""
    global _pairs_cache
    raw = get_settings().compare_pairs
    cat = catalog_store.load()
    if _pairs_cache is not None and _pairs_cache[:2] == (raw, id(cat)):
        return _pairs_cache[2]
    valid: dict[str, list[tuple[str, str]]] = {}
    for capability, listed in get_settings().compare_pair_map.items():
        adapted = {e["provider"] for e in cat.for_capability(capability)
                   if (ad := cat.adapters.get(e["id"])) is not None and ad.verified}
        for pair in listed:
            missing = [p for p in pair if p not in adapted]
            if capability not in cat.contracts or missing:
                log.warning("compare pair %s %s skipped: no verified adapter for %s", capability,
                            "/".join(pair), ", ".join(missing) or "the capability")
                continue
            valid.setdefault(capability, []).append(pair)
    _pairs_cache = (raw, id(cat), valid)
    return valid


def other_side(capability: str | None, provider: str | None) -> str | None:
    """B for a served provider A: the other side of the first valid pair naming A."""
    for x, y in pairs().get(capability or "", ()):
        if provider == x:
            return y
        if provider == y:
            return x
    return None


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
    return re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w-])",
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


def _b_target(contract, identity: dict, provider: str) -> Target | None:
    """B's endpoint for this input when treg's own key can serve it: one the router would ask for
    this input (`plan.candidates_for` and `build_plan`'s cap, declaration and scope checks; a
    judge-only `route: false` adapter builds no request), not paused, platform-eligible with a
    key, not exhausted on treg's primary account, retention allowed, a computable price and no
    second host or path parameter to resolve."""
    from httpx import QueryParams

    from ..domain.capacity.marks import spends_balance
    from ..domain.catalog.routing.plan import candidates_for, unscoped
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
    mine = [ep for ep in cat.for_capability(contract.capability) if ep["provider"] == provider]
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
        query, body = adapter.to_upstream(identity, variant)
        payload = route._child_body(ep, body)
        cost = cat.cost_view(ep.get("cost"), provider)
        if not cost or cost.get("usd") is None:
            continue
        price = _marketplace_pricing(provider, ep["id"], cost, QueryParams(query), payload)[0]
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
    """Recent successful routed parents of the paired capabilities from `org_ids` only, newest
    first, at most `SCAN_PER_CAPABILITY` each: one ordered range of (endpoint_id, created_at) per
    capability. Reading only eligible teams matters: other teams' calls can fill a newest-N
    window read over every team, leaving eligible teams' earlier marked calls unread."""
    cat = catalog_store.load()
    out: list[Candidate] = []
    if not org_ids:
        return out
    async with session_factory() as db:
        for capability in pairs():
            parent = f"treg.{capability}"
            if parent not in cat.by_id:
                continue
            rows = (await db.execute(select(CallRecord).where(
                CallRecord.endpoint_id == parent, CallRecord.created_at >= since,
                CallRecord.org_id.in_(org_ids),
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
    member or project tool ACL, no pin to a tag, no deny rule, no capability pin on a paired
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


async def _served_child(db, parent: CallRecord) -> CallRecord | None:
    """The routed parent's credited child: its latest successful attempt, never an async poll. An
    archive-served (cached) answer is stale next to a live B, so such a parent is not compared."""
    escaped = parent.call_ref.replace("/", "//").replace("%", "/%").replace("_", "/_")
    child = (await db.execute(select(CallRecord).where(
        CallRecord.org_id == parent.org_id,
        CallRecord.call_ref.startswith(f"{parent.call_ref}:r", autoescape=True),
        CallRecord.call_ref.not_like(f"{escaped}:r%:%", escape="/"),
        CallRecord.status_code >= 200, CallRecord.status_code < 300,
    ).order_by(CallRecord.id.desc()).limit(1))).scalar_one_or_none()
    if child is not None and child.cached:
        return None
    return child


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
    return org_id in await _eligible_teams(session_factory, {org_id}, set(pairs()))


async def _claim(session_factory, values: dict, limit: int, *, team_cap: int, since, ready_since) -> str:
    """In one short transaction, one attempt on the current UTC day's count (taken first: on
    Postgres its row lock orders concurrent claims), the team and pair cap read again under it, and
    the comparison row as `pending` (its unique call id stops a second run). `claimed`, `taken`,
    `gone`, `capped` or `at_limit`."""
    async with session_factory() as db:
        if await db.get(Org, values["org_id"]) is None:
            return "gone"
        if await comparisons.by_call_ref(db, values["call_ref"]) is not None:
            return "taken"
        if not await comparisons.take_attempt(db, _day(), limit):
            await db.rollback()
            return "at_limit"
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


async def _finish(session_factory, org_id: int, call_ref: str, values: dict) -> bool:
    """Settle the claimed row; take it back if its team was deleted meanwhile (team deletion itself
    removes a row it finds, and the next run removes any it missed)."""
    async with session_factory() as db:
        settled = await comparisons.finish(db, call_ref, **values)
        await db.commit()
    if not settled:
        return False
    async with session_factory() as db:
        if await db.get(Org, org_id) is None:
            await db.execute(delete(CallComparison).where(CallComparison.call_ref == call_ref))
            await db.commit()
            return False
    return True


def _day() -> str:
    return f"{utcnow_naive():%Y%m%d}"


async def _remove_orphans(session_factory) -> int:
    """Comparisons whose team is gone: team deletion removes them, this catches any it missed."""
    async with session_factory() as db:
        gone = await db.execute(delete(CallComparison).where(
            ~exists().where(Org.id == CallComparison.org_id)))
        await db.commit()
    return gone.rowcount or 0


async def collect(*, max_seconds: float = 110.0, session_factory=session_maker,
                  client: httpx.AsyncClient | None = None) -> dict:
    """One run: at most `compare_max_per_run` second-vendor calls, never past `compare_max_per_day`
    attempts for the UTC day. Sequential, `CALL_GAP_S` apart. Removes orphaned rows first, on
    every run, the feature on or off."""
    from ..domain.capacity.view import view as capacity_view
    from ..domain.catalog.routing.contracts import canonical_identity
    from .arena_insights import _evidence

    # The budget covers the whole run, choosing the teams and reading their calls included.
    deadline = time.monotonic() + max_seconds
    totals = {"read": 0, "eligible": 0, "called": 0, "ready": 0, "failed": 0, "skipped": 0,
              "removed": await _remove_orphans(session_factory), "estimate_micro": 0,
              "skip_reasons": {}, "no_marker_by_client": {}}
    settings = get_settings()
    if not configured() or settings.compare_rate <= 0:
        return {"status": "off", **totals}
    async with session_factory() as db:
        if await comparisons.attempts(db, _day()) >= settings.compare_max_per_day:
            return {"status": "at_limit", **totals}
    now = utcnow_naive()
    lookback = timedelta(hours=min(settings.compare_lookback_hours, MARKER_TTL_S // 3600))
    answered = now - timedelta(days=settings.compare_answer_days)
    teams = await _eligible_teams(session_factory, await _answering_teams(session_factory, answered),
                                  set(pairs()))
    found = await _recent(session_factory, now - lookback, teams)
    totals["read"] = len(found)
    async with session_factory() as db:
        done = set((await db.execute(select(CallComparison.call_ref).where(
            CallComparison.call_ref.in_([c.record.call_ref for c in found])))).scalars()) if found else set()
    found = [c for c in found if c.record.call_ref not in done]
    totals["eligible"] = len(found)
    cat = catalog_store.load()
    await capacity_view.load()
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=CALL_TIMEOUT_S)
    store = kv.store()
    try:
        for cand in found:
            if totals["called"] >= settings.compare_max_per_run or time.monotonic() >= deadline:
                break
            try:
                outcome = await _one(cand, cat, now, session_factory, client, store, totals,
                                     canonical_identity, _evidence)
            except Exception:
                log.warning("comparison for %s failed", cand.record.call_ref, exc_info=True)
                outcome = "skipped:error"
            if outcome == "at_limit":
                break
            outcome, _, reason = outcome.partition(":")
            totals[outcome] += 1
            if reason:
                reasons = totals["skip_reasons"]
                reasons[reason] = reasons.get(reason, 0) + 1
            if reason == "no_marker":  # which surface the unmarked calls came through
                by_client = totals["no_marker_by_client"]
                name = cand.record.client or "-"
                by_client[name] = by_client.get(name, 0) + 1
    finally:
        if own_client:
            await client.aclose()
    return {"status": "ran", **totals}


async def _one(cand, cat, now, session_factory, client, store, totals,
               canonical_identity, _evidence) -> str:
    """One candidate, to `ready`, `failed`, `skipped:<reason>` or `at_limit`. The reasons are
    counted in the run's output (`skip_reasons`), so an empty run says where candidates stopped."""
    settings = get_settings()
    parent = cand.record
    # The call path wrote this marker: the call sampled in, on a paired capability, with no
    # `X-Treg-Route-*` header. No marker, or a store that cannot say, is no comparison.
    marker = await store.get_text(marker_key(parent.call_ref))
    if marker is None:
        return "skipped:no_marker"
    try:
        given = json.loads(marker)
    except ValueError:
        given = None
    if not isinstance(given, dict):
        return "skipped:marker_without_input"
    async with session_factory() as db:
        child = await _served_child(db, parent)
        a_ep = cat.by_id.get(child.endpoint_id or "") if child is not None else None
        b_provider = other_side(cand.capability, (a_ep or {}).get("provider"))
        counted = await comparisons.counted(
            db, parent.org_id, cand.capability, (a_ep["provider"], b_provider),
            since=now - timedelta(days=settings.compare_window_days),
            ready_since=now - timedelta(days=READY_DAYS)) if b_provider else 0
    if child is None:
        return "skipped:no_served_child"
    if b_provider is None:
        return "skipped:a_not_on_a_pair"
    if child.credential_tier != "platform":
        return "skipped:a_not_on_treg_key"
    if not child.archive_key_hash:
        return "skipped:a_not_archived"
    if _retention_forbidden(child.endpoint_id):
        return "skipped:a_retention_forbidden"
    if counted >= settings.compare_team_cap:
        return "skipped:team_pair_cap"
    got = (await _evidence([child], session_factory)).get(child.id)
    contract, a_adapter = cat.contracts.get(cand.capability), cat.adapters.get(child.endpoint_id)
    if got is None:
        return "skipped:a_archive_unreadable"
    if got[2] is None:
        return "skipped:a_answer_missing"
    if contract is None or a_adapter is None:
        return "skipped:no_contract_or_adapter"
    _query, _body, answer = got
    # The question the caller asked, from the marker: no reading it back from A's request.
    identity, variant = canonical_identity(contract, dict(given))
    if variant is None:
        return "skipped:input_not_recovered"
    a_output = _output(a_ep, contract, a_adapter, 200, json.dumps(answer).encode(), identity)
    target = _b_target(contract, identity, b_provider)
    if a_output is None:
        return "skipped:a_output_unreadable"
    if target is None:
        return "skipped:b_unavailable"
    if not await _still_eligible(session_factory, parent.org_id):
        return "skipped:team_no_longer_eligible"
    claimed = await _claim(session_factory, dict(
        org_id=parent.org_id, user_email=parent.user_email, call_ref=parent.call_ref,
        capability=cand.capability, a_endpoint=child.endpoint_id, a_provider=a_ep["provider"],
        b_endpoint=target.ep["id"], b_provider=b_provider, b_cost_micro=target.price_micro),
        settings.compare_max_per_day, team_cap=settings.compare_team_cap,
        since=now - timedelta(days=settings.compare_window_days),
        ready_since=now - timedelta(days=READY_DAYS))
    if claimed == "at_limit":
        return "at_limit"
    if claimed != "claimed":
        return f"skipped:claim_{claimed}"
    if totals["called"]:
        await asyncio.sleep(CALL_GAP_S)
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
    if b_output is None:
        values = {"status": comparisons.FAILED}
    else:
        providers = [a_ep["provider"], b_provider]
        pattern = _vendor_pattern(providers, [child.endpoint_id, target.ep["id"]])
        limit = settings.compare_max_answer_bytes
        a_text, a_cut = _truncate(jsontext.dumps(_blind(a_output, pattern, set(providers))), limit)
        b_text, b_cut = _truncate(jsontext.dumps(_blind(b_output, pattern, set(providers))), limit)
        input_text, input_cut = _truncate(
            jsontext.dumps(_blind(identity, pattern, set(providers))), INPUT_MAX_BYTES)
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


async def next_for(org_id: int, *, pinned: bool) -> dict | None:
    """The team's oldest comparison waiting for a verdict, blinded; None (and the invitation flag
    cleared) when there is none. A pinned caller never reads one: comparisons carry no pin."""
    if pinned:
        return None
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


async def submit(org_id: int, *, pinned: bool, comparison_id: int, choice: str,
                 reason: str | None, client: str) -> tuple[str, bool]:
    """Record a verdict; returns (the verdict as shown, inserted). A second verdict keeps the first."""
    if pinned:
        raise ComparisonNotFound
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
