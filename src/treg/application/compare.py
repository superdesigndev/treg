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
from ..config import TREG_USER_AGENT, get_settings
from ..domain.catalog import store as catalog_store
from ..domain.feedback import comparisons
from ..infra import kv
from ..infra.db import session_maker
from ..infra.upstream.relay import relay
from ..models import CallComparison, CallRecord, CapabilityPin, DenyRule, Membership, Org
from ..timeutil import utcnow_naive

log = logging.getLogger(__name__)

READY_TTL_S = 3 * 86400
# A call's eligibility marker lives this long; a run never looks further back.
MARKER_TTL_S = 2 * 3600
MARKER_TIMEOUT_S = 0.2
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


def marker_key(call_ref: str) -> str:
    return f"compare-ok:{call_ref}"


def configured() -> bool:
    return get_settings().compare_configured


async def mark_eligible(call_ref: str, capability: str | None, raw_headers) -> None:
    """On the call path, after a successful routed answer: mark the call for a comparison run when
    it samples in, its capability is paired, and the caller sent no `X-Treg-Route-*` header (a
    caller that chose vendors or routing is left alone). One short store write, no database,
    nothing raised, nothing about the answer changed."""
    try:
        settings = get_settings()
        if (not settings.compare_configured or capability not in pairs()
                or any(bytes(name).lower().startswith(_ROUTE_HEADER_PREFIX) for name, _ in raw_headers)
                or not hints.sampled_at("compare", call_ref, settings.compare_rate)):
            return
        store = kv.store()
        async with asyncio.timeout(MARKER_TIMEOUT_S):
            await store.set_flag(marker_key(call_ref), MARKER_TTL_S)
    except Exception:  # noqa: BLE001 - a lost marker only means this call is never compared
        return


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
    """B's endpoint and the provider request its adapter builds from the contract input."""
    ep: dict
    adapter: Any
    query: dict
    body: bytes
    price_micro: int


def _b_target(contract, identity: dict, provider: str) -> Target | None:
    """B's endpoint for this input when treg's own key can serve it: a verified adapter that takes
    and scopes the identity (`route: true` is not needed), not paused, platform-eligible with a
    key, not exhausted on treg's primary account, retention allowed, a computable price and no
    second host or path parameter to resolve."""
    from httpx import QueryParams

    from ..domain.capacity.marks import spends_balance
    from ..domain.catalog.routing.contracts import adapter_accepts
    from ..domain.catalog.routing.plan import unscoped
    from .call import keyslots
    from .call.resolve import _marketplace_pricing

    cat = catalog_store.load()
    settings = get_settings()
    registered = oauth_providers.get(provider)
    if (registered is None or not registered.base_url or not registered.uses_pasted_secret
            or oauth_providers.is_paused(provider) or not settings.platform_key_for(provider)
            or not all(settings.platform_credential(b["platform_setting"], b.get("account"))
                       for b in oauth_providers.platform_bindings(registered) if b.get("platform_setting"))):
        return None
    for ep in cat.for_capability(contract.capability):
        if (ep["provider"] != provider or ep.get("status") or ep.get("async") or ep.get("host")
                or ep.get("platform_auth") == "anonymous" or "{" in str(ep.get("path") or "")):
            continue
        adapter = cat.adapters.get(ep["id"])
        if adapter is None or not adapter.verified:
            continue
        accepted = adapter_accepts(adapter, identity)
        if accepted is None or unscoped(adapter, contract, identity):
            continue
        if catalog_store.paused(ep) or _retention_forbidden(ep["id"]) or not cat.platform_eligible(ep):
            continue
        if keyslots.exhausted(provider, ep["id"], None,
                              metered=spends_balance((ep.get("cost") or {}).get("type"))):
            continue
        query, body = adapter.to_upstream(identity, accepted)
        payload = (json.dumps(body).encode()
                   if ep["method"] in ("POST", "PUT", "PATCH") and body is not None else b"")
        cost = cat.cost_view(ep.get("cost"), provider)
        if not cost or cost.get("usd") is None:
            continue
        price = _marketplace_pricing(provider, ep["id"], cost, QueryParams(query), payload)[0]
        return Target(ep=ep, adapter=adapter, query=dict(query), body=payload, price_micro=price)
    return None


async def _ask_b(target: Target, client: httpx.AsyncClient) -> tuple[int, bytes]:
    """One request to B's endpoint on treg's primary key, through the relay alone: (status, body),
    status 0 when no answer came."""
    from ..models import Tool
    from .call.resolve import QueryValues, _marketplace_upstream
    from .call.types import UpstreamRequest
    from .connect import _host_of

    provider = oauth_providers.get(target.ep["provider"])
    items = tuple((str(k), str(v)) for k, v in target.query.items())
    upstream, consumed = _marketplace_upstream(target.ep, provider, QueryValues(items))
    tool = Tool(org_id=None, name=target.ep["id"], owner="compare", base_url=provider.base_url,
                host=_host_of(provider.base_url), bindings=oauth_providers.platform_bindings(provider))
    headers = [(b"user-agent", TREG_USER_AGENT.encode())]
    if target.body:
        headers += [(b"content-type", b"application/json"),
                    (b"content-length", str(len(target.body)).encode())]

    async def body_stream():
        if target.body:
            yield target.body

    async def body_read() -> bytes:
        return target.body

    # `body_read`, as the call path offers it: a provider whose key goes in the JSON body
    # (`location: json`) is injected by reading the body whole.
    request = UpstreamRequest(method=target.ep["method"], raw_headers=tuple(headers),
                              query_items=tuple(i for i in items if i[0] not in consumed),
                              body_stream=body_stream, has_body=bool(target.body),
                              body_read=body_read if target.body else None)
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            response = await relay(request, upstream, tool, {}, client, force_identity=True)
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


def _output(ep: dict, contract, adapter, status: int, raw: bytes, identity: dict):
    """The contract output of one vendor's answer, read as the router reads a child; None unless a hit."""
    from .call import route

    outcome = route.child_outcome(ep, contract, adapter, status, raw, tier="platform", identity=identity)
    return outcome.core if outcome.kind == "hit" and outcome.core else None


@dataclass
class Candidate:
    record: CallRecord
    capability: str


async def _recent(session_factory, since: datetime) -> list[Candidate]:
    """Recent successful routed parents of the paired capabilities, newest first, at most
    `SCAN_PER_CAPABILITY` each: one ordered range of (endpoint_id, created_at) per capability."""
    cat = catalog_store.load()
    out: list[Candidate] = []
    async with session_factory() as db:
        for capability in pairs():
            parent = f"treg.{capability}"
            if parent not in cat.by_id:
                continue
            rows = (await db.execute(select(CallRecord).where(
                CallRecord.endpoint_id == parent, CallRecord.created_at >= since,
            ).order_by(CallRecord.created_at.desc()).limit(SCAN_PER_CAPABILITY))).scalars().all()
            out += [Candidate(r, capability) for r in rows
                    if r.org_id is not None and r.credential_tier == "routed" and r.call_ref
                    and 200 <= (r.status_code or 0) < 300]
    return out


async def _eligible_teams(session_factory, org_ids: set[int], capabilities: set[str]) -> set[int]:
    """Teams a comparison may run for: in the archive, not a demo, and holding no access policy at
    all (no member or project tool ACL, no pin to a tag, no deny rule, no capability pin on a
    paired capability), so B's call never needs the team's authorization emulated."""
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
    """The routed parent's credited child: its latest successful attempt, never an async poll."""
    escaped = parent.call_ref.replace("/", "//").replace("%", "/%").replace("_", "/_")
    return (await db.execute(select(CallRecord).where(
        CallRecord.org_id == parent.org_id,
        CallRecord.call_ref.startswith(f"{parent.call_ref}:r", autoescape=True),
        CallRecord.call_ref.not_like(f"{escaped}:r%:%", escape="/"),
        CallRecord.status_code >= 200, CallRecord.status_code < 300,
    ).order_by(CallRecord.id.desc()).limit(1))).scalar_one_or_none()


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


def _same_request(adapter, identity: dict, query: dict, body) -> bool:
    """A's adapter, run forward on the recovered input, builds exactly the archived request: the
    input was read back whole, with nothing an expression set from a field it cannot see."""
    from ..domain.catalog.routing.contracts import adapter_accepts

    variant = adapter_accepts(adapter, identity)
    if variant is None:
        return False
    try:
        fwd_query, fwd_body = adapter.to_upstream(identity, variant)
    except Exception:  # noqa: BLE001 - an adapter that cannot rebuild it: not the same request
        return False
    def norm_query(q):
        return {str(k): str(v) for k, v in (q or {}).items()}
    return (norm_query(fwd_query) == norm_query(query)
            and json.loads(json.dumps(fwd_body or {})) == (body or {}))


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
    from ..domain.catalog.shadow import request_identity
    from .arena_insights import _evidence

    totals = {"read": 0, "eligible": 0, "called": 0, "ready": 0, "failed": 0, "skipped": 0,
              "removed": await _remove_orphans(session_factory), "estimate_micro": 0}
    settings = get_settings()
    if not configured() or settings.compare_rate <= 0:
        return {"status": "off", **totals}
    async with session_factory() as db:
        if await comparisons.attempts(db, _day()) >= settings.compare_max_per_day:
            return {"status": "at_limit", **totals}
    now = utcnow_naive()
    lookback = timedelta(hours=min(settings.compare_lookback_hours, MARKER_TTL_S // 3600))
    found = await _recent(session_factory, now - lookback)
    totals["read"] = len(found)
    async with session_factory() as db:
        done = set((await db.execute(select(CallComparison.call_ref).where(
            CallComparison.call_ref.in_([c.record.call_ref for c in found])))).scalars()) if found else set()
    teams = await _eligible_teams(session_factory, {c.record.org_id for c in found}, set(pairs()))
    found = [c for c in found if c.record.org_id in teams and c.record.call_ref not in done]
    totals["eligible"] = len(found)
    cat = catalog_store.load()
    await capacity_view.load()
    deadline = time.monotonic() + max_seconds
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=CALL_TIMEOUT_S)
    store = kv.store()
    try:
        for cand in found:
            if totals["called"] >= settings.compare_max_per_run or time.monotonic() >= deadline:
                break
            try:
                outcome = await _one(cand, cat, now, session_factory, client, store, totals,
                                     canonical_identity, request_identity, _evidence)
            except Exception:
                log.warning("comparison for %s failed", cand.record.call_ref, exc_info=True)
                outcome = "skipped"
            if outcome == "at_limit":
                break
            totals[outcome] += 1
    finally:
        if own_client:
            await client.aclose()
    return {"status": "ran", **totals}


async def _one(cand, cat, now, session_factory, client, store, totals,
               canonical_identity, request_identity, _evidence) -> str:
    """One candidate, to `ready`, `failed`, `skipped` or `at_limit`."""
    settings = get_settings()
    parent = cand.record
    # The call path wrote this marker: the call sampled in, on a paired capability, with no
    # `X-Treg-Route-*` header. No marker, or a store that cannot say, is no comparison.
    if not await store.has_flag(marker_key(parent.call_ref)):
        return "skipped"
    async with session_factory() as db:
        child = await _served_child(db, parent)
        a_ep = cat.by_id.get(child.endpoint_id or "") if child is not None else None
        b_provider = other_side(cand.capability, (a_ep or {}).get("provider"))
        counted = await comparisons.counted(
            db, parent.org_id, cand.capability, (a_ep["provider"], b_provider),
            since=now - timedelta(days=settings.compare_window_days),
            ready_since=now - timedelta(days=READY_DAYS)) if b_provider else 0
    if (child is None or b_provider is None or child.credential_tier != "platform"
            or not child.archive_key_hash or _retention_forbidden(child.endpoint_id)
            or counted >= settings.compare_team_cap):
        return "skipped"
    got = (await _evidence([child], session_factory)).get(child.id)
    contract, a_adapter = cat.contracts.get(cand.capability), cat.adapters.get(child.endpoint_id)
    if got is None or got[2] is None or contract is None or a_adapter is None:
        return "skipped"
    query, body, answer = got
    identity, variant = canonical_identity(contract, request_identity(a_adapter, query, body))
    if variant is None or not _same_request(a_adapter, identity, query, body):
        return "skipped"
    a_output = _output(a_ep, contract, a_adapter, 200, json.dumps(answer).encode(), identity)
    target = _b_target(contract, identity, b_provider)
    if a_output is None or target is None or not await _still_eligible(session_factory, parent.org_id):
        return "skipped"
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
        return "skipped"
    if totals["called"]:
        await asyncio.sleep(CALL_GAP_S)
    totals["called"] += 1
    totals["estimate_micro"] += target.price_micro
    try:
        status, raw = await _ask_b(target, client)
    except Exception:
        log.warning("compare call %s for %s failed", target.ep["id"], parent.call_ref, exc_info=True)
        status, raw = 0, b""
    log.info("compare call %s for %s: status %s, catalog price %s micro-USD",
             target.ep["id"], parent.call_ref, status, target.price_micro)
    b_output = _output(target.ep, contract, target.adapter, status, raw, identity) if status else None
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
        return "skipped"
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
