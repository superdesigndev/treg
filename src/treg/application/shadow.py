"""The shadow provider run: ask a candidate the same question a served call asked, after the fact,
and record how the two answers compare (`domain.catalog.shadow` holds the rules).

It reads the audit table and the archive the way the arena collector does, so it is never on a
customer's request path and never touches a team's balance: the candidate is called with its own
key, from the worker, and its answer is compared and dropped. Only yes/no comparisons, counts,
timings and a salted subject hash are stored. Off unless `shadow_provider` is set.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from datetime import timedelta

import httpx
from sqlalchemy import delete, func, select

from ..config import get_settings
from ..domain.catalog import shadow as rules, store
from ..infra.db import session_maker
from ..models import CallRecord, ShadowCursor, ShadowPair
from ..timeutil import utcnow_naive as now
from .arena_insights import _evidence

log = logging.getLogger(__name__)
CURSOR = "callrecord"
BATCH = 1000


def spec() -> rules.Spec | None:
    raw = get_settings().shadow_provider
    try:
        return rules.parse(raw)
    except (ValueError, KeyError, TypeError):
        log.warning("shadow_provider setting is not valid; the shadow run is off")
        return None


def _served_endpoints(sp: rules.Spec) -> dict[str, str]:
    """Served endpoint id → capability, for every routed adapter of a capability the spec names."""
    cat = store.load()
    out = {}
    for eid in cat.adapters:
        ep = cat.by_id.get(eid)
        if ep is not None and ep.get("capability") in sp.targets and ep.get("provider") != "treg":
            out[eid] = ep["capability"]
    return out


async def _cursor(session_factory) -> int | None:
    """The last consumed call id, or None on the first run (which only records where to start)."""
    async with session_factory() as db:
        row = await db.get(ShadowCursor, CURSOR)
        if row is not None:
            return row.cursor_id
        newest = (await db.execute(select(func.max(CallRecord.id)))).scalar() or 0
        db.add(ShadowCursor(id=CURSOR, cursor_id=newest))
        await db.commit()
        return None


async def _ask(client: httpx.AsyncClient, sp: rules.Spec, target: rules.Target, request, key: str):
    query, body = request
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    started = time.monotonic()
    try:
        r = await client.request(target.method, sp.base_url + target.path, params=query or None,
                                 json=body if body else None, headers=headers)
    except httpx.HTTPError:
        return 0, None, int((time.monotonic() - started) * 1000), None
    ms = int((time.monotonic() - started) * 1000)
    units = None
    if sp.units_header:
        try:
            units = float(r.headers.get(sp.units_header, ""))
        except ValueError:
            units = None
    try:
        doc = r.json()
    except ValueError:
        doc = None
    return r.status_code, doc, ms, units


def _list_count(fields: dict) -> int | None:
    explicit = rules.count(fields, "count")
    if explicit is not None:
        return explicit
    lists = [v for v in fields.values() if isinstance(v, list)]
    return len(lists[0]) if lists else None


async def collect(*, max_seconds: float = 110.0, session_factory=session_maker,
                  client: httpx.AsyncClient | None = None) -> dict:
    sp = spec()
    if sp is None:
        return {"status": "off"}
    s = get_settings()
    served = _served_endpoints(sp)
    cursor = await _cursor(session_factory)
    if cursor is None:
        return {"status": "started"}
    cat = store.load()
    deadline = time.monotonic() + max_seconds
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=s.shadow_timeout_s)
    gate = asyncio.Semaphore(max(1, s.shadow_concurrency))
    gap = 1.0 / max(0.1, s.shadow_per_second)
    totals = {"read": 0, "paired": 0, "not_mappable": 0, "skipped": 0}
    try:
        while time.monotonic() < deadline:
            async with session_factory() as db:
                records = (await db.execute(select(CallRecord).where(
                    CallRecord.id > cursor, CallRecord.endpoint_id.in_(list(served)))
                    .order_by(CallRecord.id).limit(BATCH))).scalars().all()
            if not records:
                break
            picked = [r for r in records if (200 <= (r.status_code or 0) < 300 or r.status_code == 404)
                      and rules.sampled(served[r.endpoint_id], r.id, sp, s.shadow_salt)]
            totals["read"] += len(records)
            evidence = await _evidence(picked, session_factory) if picked else {}
            pairs: list[ShadowPair] = []

            async def one(r, evidence=evidence, pairs=pairs):
                cap = served[r.endpoint_id]
                target = sp.targets[cap]
                got = evidence.get(r.id)
                if got is None:
                    totals["skipped"] += 1
                    return
                query, body, answer = got
                identity = rules.request_identity(cat.adapters[r.endpoint_id], query, body)
                request = rules.shadow_request(target, identity)
                if request is None:
                    totals["not_mappable"] += 1
                    return
                served_found, served_fields = rules.read(cat.adapters[r.endpoint_id], answer)
                async with gate:
                    await asyncio.sleep(gap)
                    status, doc, ms, units = await _ask(client, sp, target, request, s.shadow_provider_key)
                shadow_found, shadow_fields = (rules.read(target.adapter, doc) if 200 <= status < 300
                                               else (False, {}))
                pairs.append(ShadowPair(
                    call_id=r.id, capability=cap, endpoint_id=r.endpoint_id,
                    subject=rules.subject_hash(identity, s.shadow_salt),
                    served_found=served_found, shadow_found=shadow_found,
                    served_count=_list_count(served_fields) if served_found else None,
                    shadow_count=_list_count(shadow_fields) if shadow_found else None,
                    served_with_email=rules.count(served_fields, "with_email"),
                    shadow_with_email=rules.count(shadow_fields, "with_email"),
                    fields=rules.compare(served_fields, shadow_fields) if served_found and shadow_found else {},
                    shadow_status=status, shadow_ms=ms if status else None, shadow_units=units,
                    served_cost_micro=r.cost_charged_micro or 0))

            await asyncio.gather(*(one(r) for r in picked))
            cursor = records[-1].id
            async with session_factory() as db:
                db.add_all(pairs)
                row = await db.get(ShadowCursor, CURSOR)
                row.cursor_id, row.updated_at = cursor, now()
                await db.commit()
            totals["paired"] += len(pairs)
        async with session_factory() as db:
            await db.execute(delete(ShadowPair).where(
                ShadowPair.created_at < now() - timedelta(days=max(1, s.shadow_retention_days))))
            await db.commit()
    finally:
        if own_client:
            await client.aclose()
    return {"status": "ran", "cursor": cursor, **totals}


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


async def report(*, days: int = 1, session_factory=session_maker) -> dict:
    """Per capability and served endpoint: hit rates, agreement, email, speed and cost."""
    since = now() - timedelta(days=max(1, days))
    groups: dict[tuple[str, str], dict] = {}
    async with session_factory() as db:
        rows = await db.stream_scalars(select(ShadowPair).where(ShadowPair.created_at >= since)
                                       .execution_options(yield_per=2000))
        async for p in rows:
            g = groups.setdefault((p.capability, p.endpoint_id), {
                "pairs": 0, "answered": 0, "served_found": 0, "shadow_found": 0, "both_found": 0,
                "shadow_found_with_email": 0, "shadow_errors": 0, "ms": [], "units": 0.0,
                "served_cost_micro": 0, "fields": {}})
            g["pairs"] += 1
            # A refusal, a timeout or a server error is no answer, not a miss: those pairs count in
            # the error rate and stay out of every hit rate.
            if p.shadow_status == 0 or p.shadow_status == 429 or p.shadow_status >= 500:
                g["shadow_errors"] += 1
                continue
            g["answered"] += 1
            g["served_found"] += p.served_found
            g["shadow_found"] += p.shadow_found
            g["both_found"] += p.served_found and p.shadow_found
            g["shadow_found_with_email"] += bool(p.shadow_found and (p.shadow_with_email or 0) > 0)
            if p.shadow_ms is not None:
                g["ms"].append(p.shadow_ms)
            g["units"] += p.shadow_units or 0
            g["served_cost_micro"] += p.served_cost_micro
            for name, ok in (p.fields or {}).items():
                f = g["fields"].setdefault(name, [0, 0])
                f[0] += bool(ok)
                f[1] += 1
    out = []
    for (cap, eid), g in sorted(groups.items()):
        ms = sorted(g.pop("ms"))
        n, a = g["pairs"], g["answered"]
        out.append({
            "capability": cap, "served_endpoint": eid, "pairs": n, "answered": a,
            "served_hit_pct": _pct(g["served_found"], a), "shadow_hit_pct": _pct(g["shadow_found"], a),
            "both_found": g["both_found"],
            "shadow_hit_with_email_pct": _pct(g["shadow_found_with_email"], a),
            "shadow_error_pct": _pct(g["shadow_errors"], n),
            "shadow_p50_ms": statistics.median(ms) if ms else None,
            "shadow_p95_ms": ms[int(0.95 * (len(ms) - 1))] if ms else None,
            "agreement_pct": {k: _pct(a, t) for k, (a, t) in sorted(g["fields"].items())},
            "shadow_units_per_1k": round(1000 * g["units"] / n, 2) if n else None,
            "served_usd_per_1k": round(g["served_cost_micro"] / n / 1000, 4) if n else None,
        })
    return {"since": since.isoformat() + "Z", "days": days, "rows": out}


def dumps(result: dict) -> str:
    return json.dumps(result, sort_keys=True)
