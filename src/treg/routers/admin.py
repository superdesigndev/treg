"""Cross-tenant admin read and reconciliation routes."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_, text
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import reconcile
from ..application import evidence_retention
from ..config import get_settings
from ..infra import kv
from ..infra.db import get_admin_read_session, get_admin_session
from ..domain import money
from ..models import ArchiveEndpointStat, ArchiveKey, ArchiveSnapshot, Bundle, CallRecord, EndpointDayStat, LedgerEntry, Membership, Org, Referral, Secret, Tool, User
from ..timeutil import as_naive as _as_naive
from ..timeutil import utcnow_naive as _utcnow_naive
from ..domain.identity.access import require_superadmin
from ..domain.governance.teams import cascade_delete_org, delete_membership, drop_member_deny_rules


# The app alias preserves the moved handlers' original @app.get decorator text byte-for-byte.
app = APIRouter()
reads_router = app


def _tally(items) -> dict:
    d: dict[str, int] = {}
    for k in items:
        d[k] = d.get(k, 0) + 1
    return d


@app.get("/admin/stats")
async def admin_stats(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)) -> dict:
    """Platform totals for the admin page. Every count runs in the database.

    Loading the whole call table to count it in Python ran past the request timeout at production
    size, so the page got a 502. Call counts now come from `endpointdaystat` (one row per endpoint
    per day, folded by the `treg-worker catalog stats` cron): calls the provider saw, as of that
    job's last run. The all-time total is Postgres's own row estimate, not an exact count.
    """
    now = _utcnow_naive()
    demo_orgs = select(Org.id).where(Org.demo)

    async def n(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    def recent(model, days: int):
        return model.created_at >= now - timedelta(days=days)

    users = select(func.count()).select_from(User).where(User.demo.is_(False))
    orgs = select(func.count()).select_from(Org).where(Org.demo.is_(False))
    # `or_(is_(None))`: NOT IN drops a NULL org_id, and a tool or secret with no team still counts.
    tools = (await db.execute(select(Tool).where(
        or_(Tool.org_id.is_(None), Tool.org_id.not_in(demo_orgs))))).scalars().all()
    secret_health = (await db.execute(select(Secret.health_status).where(
        or_(Secret.org_id.is_(None), Secret.org_id.not_in(demo_orgs))))).scalars().all()

    async def calls_since(days: int) -> tuple[int, int]:
        day = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        row = (await db.execute(select(func.sum(EndpointDayStat.n), func.sum(EndpointDayStat.ok))
                                .where(EndpointDayStat.day >= day))).one()
        return int(row[0] or 0), int(row[1] or 0)

    calls_7d, _ok_7d = await calls_since(7)
    calls_30d, ok_30d = await calls_since(30)
    if db.bind.dialect.name == "postgresql":
        total_calls = await n(text(
            "SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE oid = 'callrecord'::regclass"))
    else:
        total_calls = await n(select(func.count()).select_from(CallRecord))
    return {
        "totals": {
            "users": await n(users), "orgs": await n(orgs), "tools": len(tools),
            "secrets": len(secret_health), "bundles": await n(select(func.count()).select_from(Bundle)),
            "calls": total_calls,
            "superadmins": await n(users.where(User.is_superadmin)),
            "suspended_orgs": await n(orgs.where(Org.suspended)),
        },
        "tools_by_injector": _tally(b.get("injector", "?") for t in tools for b in t.bindings),
        "tools_by_host": _tally(t.host for t in tools),
        "credential_health": _tally(secret_health),
        "calls": {
            "last_7d": calls_7d, "last_30d": calls_30d, "total": total_calls,
            "success_rate": round(ok_30d / calls_30d, 3) if calls_30d else None,
        },
        "growth": {
            "new_users_7d": await n(users.where(recent(User, 7))),
            "new_users_30d": await n(users.where(recent(User, 30))),
            "new_orgs_7d": await n(orgs.where(recent(Org, 7))),
            "new_orgs_30d": await n(orgs.where(recent(Org, 30))),
        },
    }


@app.get("/admin/orgs")
async def admin_orgs(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)) -> list[dict]:
    orgs = (await db.execute(select(Org))).scalars().all()

    async def _counts(model) -> dict[int, int]:  # one grouped COUNT instead of one-per-org (was O(orgs) queries)
        rows = await db.execute(select(model.org_id, func.count()).group_by(model.org_id))
        return {oid: n for oid, n in rows.all()}

    mems: dict[int, list] = {}
    for m in (await db.execute(select(Membership))).scalars().all():
        mems.setdefault(m.org_id, []).append(m)
    tool_n, secret_n, bundle_n = await _counts(Tool), await _counts(Secret), await _counts(Bundle)
    # Was 1 + 4N serial queries (401 at 100 orgs); now a constant ~4 regardless of tenant count.
    return [
        {
            "id": o.id, "slug": o.slug, "name": o.name, "suspended": o.suspended,
            "members": len(mems.get(o.id, [])), "roles": _tally(m.role for m in mems.get(o.id, [])),
            "tools": tool_n.get(o.id, 0), "secrets": secret_n.get(o.id, 0), "bundles": bundle_n.get(o.id, 0),
            "created_at": o.created_at.isoformat(),
        }
        for o in orgs
    ]


@app.get("/admin/orgs/{org_id}")
async def admin_org_detail(
    org_id: int, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    mem = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    umap = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_([m.user_id for m in mem]))
    )).scalars().all()}  # batched (was one db.get per member)
    members = [{"user_id": m.user_id, "email": umap[m.user_id].email if m.user_id in umap else None, "role": m.role}
               for m in mem]
    tools = (await db.execute(select(Tool).where(Tool.org_id == org_id))).scalars().all()
    secrets = (await db.execute(select(Secret).where(Secret.org_id == org_id))).scalars().all()
    recent = (
        await db.execute(select(CallRecord).where(CallRecord.org_id == org_id).order_by(CallRecord.id.desc()).limit(20))
    ).scalars().all()
    return {
        "id": org.id, "slug": org.slug, "name": org.name, "suspended": org.suspended,
        "members": members,
        "tools": [{"id": t.id, "name": t.name, "host": t.host, "owner": t.owner,
                   "injectors": [b.get("injector") for b in t.bindings]} for t in tools],
        "secrets": [{"id": s.id, "name": s.name, "kind": s.kind, "health": s.health_status, "owner": s.owner} for s in secrets],
        "recent_calls": [{"tool": c.tool_name, "method": c.method, "status": c.status_code,
                          "user": c.user_email, "at": c.created_at.isoformat()} for c in recent],
    }


@app.get("/admin/users")
async def admin_users(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)) -> list[dict]:
    users = (await db.execute(select(User))).scalars().all()
    mems_by_user: dict[int, list] = {}  # all memberships in one query, grouped (was one query per user)
    for m in (await db.execute(select(Membership))).scalars().all():
        mems_by_user.setdefault(m.user_id, []).append(m)
    omap = {o.id: o for o in (await db.execute(  # all referenced orgs in one query (was one db.get per membership)
        select(Org).where(Org.id.in_({m.org_id for ms in mems_by_user.values() for m in ms}))
    )).scalars().all()}
    return [
        {
            "id": u.id, "email": u.email, "is_superadmin": u.is_superadmin, "suspended": u.suspended,
            "orgs": [{"slug": omap[m.org_id].slug if m.org_id in omap else None, "role": m.role}
                     for m in mems_by_user.get(u.id, [])],
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


@app.get("/admin/tools")
async def admin_tools(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)) -> list[dict]:
    tools = (await db.execute(select(Tool))).scalars().all()
    omap = {o.id: o for o in (await db.execute(  # batched (was one db.get per tool)
        select(Org).where(Org.id.in_({t.org_id for t in tools}))
    )).scalars().all()}
    return [{"id": t.id, "name": t.name, "org": omap[t.org_id].slug if t.org_id in omap else None,
             "host": t.host, "owner": t.owner, "injectors": [b.get("injector") for b in t.bindings]} for t in tools]


@app.get("/admin/share")
async def admin_share(
    minutes: int = 60, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    """Who served each job over the last `minutes` (at most 360). `requests` counts what callers
    asked (direct calls and routed parents, never a routed attempt). `by_provider` credits the
    answer a caller actually got: a direct call's 2xx that its adapter did not judge a miss
    (`CallRecord.hit` False), and, for a routed call, only the LAST attempt of a parent that
    succeeded - the attempt whose answer the job returned. An earlier attempt the job moved on
    from is not credited, whatever its status: a free provider's body is often streamed without
    a `hit` verdict, so its empty 200s would otherwise count. Two reads over an id range.

    `teams` counts the same answers per team, so one heavy team cannot stand for the market:
    `total` teams with an answer, `by_provider` teams a provider served, `weighted` each
    provider's share averaged over teams (every team one vote), and `direct` / `routed` the same
    team counts for calls that named a provider and for calls treg's job served."""
    from ..application import catalog_stats
    from ..domain.catalog import store as catalog_store
    minutes = max(1, min(minutes, 360))
    since = _utcnow_naive() - timedelta(minutes=minutes)
    first = await catalog_stats._first_id_at(db, since)
    attempt = CallRecord.call_ref.like("%:r%")
    ok = case((and_(CallRecord.status_code.between(200, 299), or_(CallRecord.hit.is_(None), CallRecord.hit.is_(True))), 1),
              else_=0)
    rows = (await db.execute(
        select(CallRecord.endpoint_id, CallRecord.provider, CallRecord.org_id, func.count(), func.sum(ok))
        .where(CallRecord.id >= first, CallRecord.endpoint_id.is_not(None), ~attempt)
        .group_by(CallRecord.endpoint_id, CallRecord.provider, CallRecord.org_id))).all()
    by_id = catalog_store.load().by_id

    def capability(endpoint_id: str) -> str:
        cap = (by_id.get(endpoint_id) or {}).get("capability")
        return cap or (endpoint_id[len("treg."):] if endpoint_id.startswith("treg.") else "(none)")

    jobs: dict[str, dict] = {}
    # (capability, org, provider, routed?) -> answers served
    served: dict[tuple[str, int, str, bool], int] = {}

    def credit(cap: str, org: int, provider: str, routed: bool, n: int) -> None:
        job = jobs.setdefault(cap, {"requests": 0, "answered": 0, "by_provider": {}})
        job["by_provider"][provider] = job["by_provider"].get(provider, 0) + n
        served[(cap, org, provider, routed)] = served.get((cap, org, provider, routed), 0) + n

    for endpoint_id, provider, org, n, n_ok in rows:
        job = jobs.setdefault(capability(endpoint_id), {"requests": 0, "answered": 0, "by_provider": {}})
        job["requests"] += n
        job["answered"] += int(n_ok or 0)
        if provider and provider != "treg" and n_ok:
            credit(capability(endpoint_id), org, provider, False, int(n_ok))
    # Routed attempts: the last attempt of each parent that answered 2xx is the one served.
    parents_ok = {ref for (ref,) in (await db.execute(
        select(CallRecord.call_ref).where(
            CallRecord.id >= first, CallRecord.endpoint_id.like("treg.%"), ~attempt,
            CallRecord.status_code.between(200, 299)))).all()}
    last: dict[str, tuple[int, str, str | None, int, bool | None, int]] = {}
    for ref, endpoint_id, provider, status, hit, org in (await db.execute(
            select(CallRecord.call_ref, CallRecord.endpoint_id, CallRecord.provider,
                   CallRecord.status_code, CallRecord.hit, CallRecord.org_id)
            .where(CallRecord.id >= first, CallRecord.endpoint_id.is_not(None), attempt))).all():
        parent, _, n = ref.rpartition(":r")
        if parent not in parents_ok or not n.isdigit():
            continue
        if parent not in last or int(n) > last[parent][0]:
            last[parent] = (int(n), endpoint_id, provider, status, hit, org)
    for _, endpoint_id, provider, status, hit, org in last.values():
        if provider and provider != "treg" and 200 <= status < 300 and hit is not False:
            credit(capability(endpoint_id), org, provider, True, 1)
    for cap, job in jobs.items():
        per_org: dict[int, dict[str, int]] = {}
        sides: dict[bool, dict[str, set]] = {False: {}, True: {}}
        side_orgs: dict[bool, set] = {False: set(), True: set()}
        for (c, org, provider, routed), n in served.items():
            if c != cap:
                continue
            per_org.setdefault(org, {})[provider] = per_org.get(org, {}).get(provider, 0) + n
            sides[routed].setdefault(provider, set()).add(org)
            side_orgs[routed].add(org)
        weighted: dict[str, float] = {}
        for split in per_org.values():
            total = sum(split.values())
            for provider, n in split.items():
                weighted[provider] = weighted.get(provider, 0.0) + n / total / len(per_org)
        users: dict[str, int] = {}
        for split in per_org.values():
            for provider in split:
                users[provider] = users.get(provider, 0) + 1
        job["teams"] = {
            "total": len(per_org), "by_provider": users,
            "weighted": {p: round(w, 4) for p, w in weighted.items()},
            "direct": {"total": len(side_orgs[False]), "by_provider": {p: len(o) for p, o in sides[False].items()}},
            "routed": {"total": len(side_orgs[True]), "by_provider": {p: len(o) for p, o in sides[True].items()}},
        }
    ordered = sorted(jobs.items(), key=lambda kv: -kv[1]["requests"])
    return {"since": since.isoformat(), "minutes": minutes,
            "jobs": [{"capability": cap, **job} for cap, job in ordered]}


def _error_reason(text: str | None) -> str | None:
    """A short failure reason from a stored error answer: the upstream's own `reason`, `error`,
    `code` or `message` when the body is JSON, else its first characters."""
    if not text or text == _ERROR_EVIDENCE_EXPIRED:
        return None
    body = re.sub(r"^\[[^\]]*\]\s*", "", text).strip()
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        for key in ("reason", "error", "code", "message"):
            value = doc.get(key)
            if isinstance(value, dict):
                value = value.get("code") or value.get("message")
            if isinstance(value, str) and value:
                return value[:120]
    return body[:120] or None


@app.get("/admin/calls")
async def admin_calls(
    limit: int = 50, since_id: int | None = None, provider: str | None = None,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> list[dict]:
    """The newest calls, or with `since_id` the calls after that id, oldest first, so a poller
    advances its cursor to the last id it read. `provider` needs `since_id`: it keeps the read a
    range over the primary key instead of a walk back through the whole table. A failed call carries
    `error_reason`, the upstream's own short reason from its stored error answer."""
    limit = max(1, min(limit, 1000))
    if provider and since_id is None:
        raise HTTPException(422, "provider needs since_id")
    q = select(CallRecord)
    if since_id is not None:
        q = q.where(CallRecord.id > since_id).order_by(CallRecord.id.asc())
    else:
        q = q.order_by(CallRecord.id.desc())
    if provider:
        q = q.where(CallRecord.provider == provider)
    rows = (await db.execute(q.limit(limit))).scalars().all()
    return [{"id": c.id, "org_id": c.org_id, "user": c.user_email, "tool": c.tool_name,
             "method": c.method, "status": c.status_code, "at": c.created_at.isoformat(),
             "endpoint_id": c.endpoint_id, "provider": c.provider, "tier": c.credential_tier,
             "call_ref": c.call_ref, "charged_micro": c.cost_charged_micro,
             "observed_micro": c.cost_observed_micro, "duration_ms": c.duration_ms,
             "upstream_ms": c.upstream_ms, "cached": c.cached,
             "error_reason": _error_reason(c.error_response)} for c in rows]


_ERROR_EVIDENCE_TTL_DAYS = evidence_retention.ERROR_EVIDENCE_TTL_DAYS
_ERROR_EVIDENCE_EXPIRED = evidence_retention.ERROR_EVIDENCE_EXPIRED


@app.get("/admin/errors")
async def admin_errors(
    days: int = 7, limit: int = 100, provider: str | None = None, status: int | None = None,
    tier: str | None = None,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_read_session),
) -> dict:
    """Failed calls with the evidence to explain them — the caller's request and the provider's own
    answer (see models.CallRecord.error_request).

    Superadmin-only and deliberately not mirrored on `/calls`: the rows hold customers' request
    content, so v1 keeps them behind the same door as every other cross-tenant view.

    Read-only. Ageing is the `treg-worker admin purge-evidence` cron's job
    (application/evidence_retention.py); until it has run, a row older than the window is shown as
    expired with no evidence, so a late schedule never widens what this view reveals.
    Evidence and org names may lag when a read database is configured; authorization stays on
    the primary.
    """
    cutoff = evidence_retention.cutoff()
    since = _utcnow_naive() - timedelta(days=max(1, min(days, 90)))
    q = (select(CallRecord)
         .where(CallRecord.created_at >= since,
                or_(CallRecord.error_request.is_not(None), CallRecord.error_response.is_not(None)))
         .order_by(CallRecord.id.desc()).limit(max(1, min(limit, 500))))
    if provider:
        q = q.where(CallRecord.provider == provider)
    if status is not None:
        q = q.where(CallRecord.status_code == status)
    if tier is not None:
        q = q.where(CallRecord.credential_tier.is_(None) if tier == ""
                    else CallRecord.credential_tier == tier)
    rows = (await db.execute(q)).scalars().all()
    omap = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_({c.org_id for c in rows if c.org_id is not None})))).scalars().all()}
    return {
        "since": since.isoformat(), "days": days, "retention_days": _ERROR_EVIDENCE_TTL_DAYS,
        "errors": [{
            "id": c.id, "call_ref": c.call_ref, "at": c.created_at.isoformat(),
            "org": omap[c.org_id].slug if c.org_id in omap else None,
            # `method` is already on the row and is the whole diagnosis for a real failure class:
            # 47 of apollo.people.enrich's failures were a GET at a POST endpoint. Omitting a field
            # we already store, in the view built to explain failures, was a free loss.
            "endpoint_id": c.endpoint_id, "provider": c.provider, "tier": c.credential_tier,
            "status": c.status_code,
            "method": c.method, "refused_by": c.refused_by, "duration_ms": c.duration_ms,
            # An aged-out row holds the sentinel, which is a STATE, not content. Returning it as the
            # request/response would have a reader treat the word `<expired>` as the provider's
            # answer; `expired` says the same thing without pretending to be evidence.
            "request": None if expired else c.error_request,
            "response": None if expired else c.error_response,
            "expired": expired,
        } for c in rows
          for expired in (c.error_response == _ERROR_EVIDENCE_EXPIRED or c.created_at < cutoff,)],
    }


@app.get("/admin/health")
async def admin_health(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.health_status != "ok"))).scalars().all()
    omap = {o.id: o for o in (await db.execute(  # batched (was one db.get per secret)
        select(Org).where(Org.id.in_({s.org_id for s in rows}))
    )).scalars().all()}
    out: list[dict] = []
    for s in rows:
        org = omap.get(s.org_id)
        out.append({"secret_id": s.id, "name": s.name, "org": org.slug if org else None,
                    "kind": s.kind, "status": s.health_status, "detail": s.health_detail})
    return out


@app.get("/admin/kv", include_in_schema=False)
async def admin_kv(_: str = Depends(require_superadmin)) -> dict:
    """Is the shared key-value store reachable? `configured` false means the in-process fallback
    (no `TREG_KV_URL`); `reachable` false with it configured means every budgeted invitation is
    currently withheld (infra/kv.py fails closed)."""
    return {"configured": kv.configured(), "reachable": await kv.store().ping()}


# Rebind app so the second block keeps its decorator text and remains a separate attach point.
app = APIRouter()
reports_router = app


# ---- reconciliation: is the money real? ----------------------------------------------------
# Cross-org aggregates over platform spend, so `require_superadmin` and nothing weaker — an org admin
# may see their own bill (`/orgs/{id}/balance`), never the platform's margin. See reconcile.py.
@app.get("/admin/reconcile/drift")
async def admin_reconcile_drift(
    since_days: int = 30, min_calls: int = 3,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """Endpoints whose observed cost has wandered from the catalog's estimate. Only the providers that
    report their own charge in-band appear (see `reconcile.price_drift`)."""
    since = reconcile.window_start(since_days)
    rows = await reconcile.price_drift(db, since, max(1, min_calls))
    return {"since": since.isoformat(), "since_days": since_days, "min_calls": min_calls,
            "tolerance": reconcile.DRIFT_TOLERANCE,
            "flagged": [r for r in rows if r["flagged"]], "endpoints": rows}


@app.get("/admin/reconcile/spend")
async def admin_reconcile_spend(
    since_days: int = 30, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """Settled platform spend per provider — the number to hold next to the provider's own invoice."""
    since = reconcile.window_start(since_days)
    return {"since": since.isoformat(), "since_days": since_days,
            **await reconcile.provider_spend(db, since)}


@app.get("/admin/reconcile/repeats")
async def admin_reconcile_repeats(
    since_days: int = 30, top: int = 10,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """How much of the bill was the same query twice — the cache-worthiness measurement."""
    since = reconcile.window_start(since_days)
    return {"since": since.isoformat(), "since_days": since_days,
            **await reconcile.repeat_rate(db, since, top=top)}


@app.get("/admin/reconcile/asynctasks")
async def admin_reconcile_asynctasks(
    since_days: int = 30,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """Deferred holds, absorbed timeouts, usage overruns (team paid more than reserved), block
    shortfalls (platform absorbed), and provider task outcomes."""
    since = reconcile.window_start(since_days)
    return {"since": since.isoformat(), "since_days": since_days,
            **await reconcile.async_task_settlement(db, since)}


_ARCHIVE_REPORT_TTL_S = 30
_archive_report_cache: dict = {}


@app.get("/admin/archive")
async def admin_archive(
    top: int = 50,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """Phase 0's read-out, served from the recorder-maintained running totals
    (ArchiveEndpointStat): ~50 tiny rows instead of walking every key and snapshot — the old
    aggregates cost 9.9s + 8.0s + 14.8s per cold load at 434k keys on prod (2026-09-03).
    `refetches` = stable + changed; `change_ratio` = changed/refetches. The 30s in-process cache
    stays as the belt against poll stacking."""
    from .. import archive as archive_mod

    import time as _time
    hit = _archive_report_cache.get(top)
    if hit and _time.monotonic() - hit[0] < _ARCHIVE_REPORT_TTL_S:
        return hit[1] | {"change_outcomes": dict(archive_mod.change_outcomes),
                         "body_outcomes": dict(archive_mod.archive_bodies.outcomes)}

    stats = (await db.execute(
        select(ArchiveEndpointStat)
        .order_by((ArchiveEndpointStat.stable + ArchiveEndpointStat.changed).desc(),
                  ArchiveEndpointStat.keys.desc())
        .limit(max(1, min(top, 500))))).scalars().all()
    totals = (await db.execute(
        select(func.coalesce(func.sum(ArchiveEndpointStat.keys), 0),
               func.coalesce(func.sum(ArchiveEndpointStat.snapshots), 0),
               func.coalesce(func.sum(ArchiveEndpointStat.bodies_kept), 0),
               func.coalesce(func.sum(ArchiveEndpointStat.kept_bytes), 0)))).one()

    hit_rows = (await db.execute(
        select(CallRecord.endpoint_id, func.count(CallRecord.id))
        .where(CallRecord.cached.is_(True)).group_by(CallRecord.endpoint_id))).all()
    hits_by_ep = {ep: int(n) for ep, n in hit_rows}
    today = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    hits_today = (await db.execute(
        select(func.count(CallRecord.id))
        .where(CallRecord.cached.is_(True), CallRecord.created_at >= today))).scalar_one()
    refreshes_today = (await db.execute(
        select(func.count(ArchiveSnapshot.id))
        .where(ArchiveSnapshot.origin == "refresh",
               ArchiveSnapshot.fetched_at >= today))).scalar_one()

    rows = []
    for st in stats:
        refetches = st.stable + st.changed
        rows.append({
            "endpoint_id": st.endpoint_id, "provider": st.provider, "policy": st.policy,
            "keys": st.keys, "refetches": refetches,
            "stable": st.stable, "changed": st.changed,
            "change_ratio": round(st.changed / refetches, 4) if refetches else None,
            "newest_fetch": st.newest_fetch.isoformat() if st.newest_fetch else None,
            "hits": hits_by_ep.get(st.endpoint_id, 0),
            "kept_bytes": st.kept_bytes,
        })
    report = {"mode": archive_mod.mode(),
              "change_outcomes": dict(archive_mod.change_outcomes),
              "body_outcomes": dict(archive_mod.archive_bodies.outcomes),
              "comparison_mode": "json",
              "ttl_policy": "adaptive",
              "serve_endpoints": sorted(archive_mod.serve_endpoints()),
              "serve_percent": get_settings().archive_serve_percent,
              "serve_max_age_s": get_settings().archive_serve_max_age_s,
              # Cumulative counters include observations from older comparison policies.
              "change_statistics_scope": "lifetime_mixed_comparison_modes",
            "worker_on": archive_mod.worker_enabled(),
            "refresh_daily_cap": get_settings().archive_refresh_daily_cap,
            "keys": int(totals[0]), "snapshots": int(totals[1]),
            "bodies_kept": int(totals[2]), "kept_bytes": int(totals[3]),
            "hits_today": int(hits_today), "refreshes_today": int(refreshes_today),
            "endpoints": rows}
    _archive_report_cache[top] = (_time.monotonic(), report)
    return report


@app.get("/admin/archive/keys")
async def admin_archive_keys(
    endpoint_id: str, limit: int = 20,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """The panel's inspector feed: this endpoint's keys (most recently demanded first), each with
    its timer state and its last versions, plus the endpoint's recent call events (served hits and
    live calls, from the audit rows). Read-only; superadmin like every /admin route."""
    from ..models import ArchiveKey as AK
    from ..models import ArchiveSnapshot as AS

    limit = max(1, min(limit, 100))
    keys = (await db.execute(
        select(AK).where(AK.endpoint_id == endpoint_id)
        .order_by(AK.last_requested_at.desc().nulls_last(), AK.fetched_at.desc())
        .limit(limit))).scalars().all()
    # ONE columns-only query for every key's recent versions — the previous shape ran a query
    # per key AND selected whole rows, dragging every stored BODY out of the database just to
    # report whether one exists (12 bodies × up to 100 keys × the panel's fan-out = the
    # 2026-09-03 pool-pressure repeat). `body IS NOT NULL` reads the row header, never the bytes.
    key_ids = [k.id for k in keys]
    vrows = (await db.execute(
        select(AS.key_id, AS.version, AS.origin, AS.size_bytes, AS.fetched_at,
               AS.body_of, AS.body.is_not(None))
        .where(AS.key_id.in_(key_ids))
        .order_by(AS.key_id, AS.version.desc()).limit(12 * max(1, len(key_ids))))).all()
    vers_by_key: dict[int, list] = {}
    for kid, version, origin, size_bytes, fetched_at, body_of, has_body in vrows:
        bucket = vers_by_key.setdefault(kid, [])
        if len(bucket) < 12:
            bucket.append((version, origin, size_bytes, fetched_at, body_of, has_body))
    out_keys = []
    for k in keys:
        vers = vers_by_key.get(k.id, [])
        out_keys.append({
            "key_hash": k.key_hash, "ttl_s": k.ttl_s, "policy": k.policy,
            "stable": k.stable_seen, "changed": k.change_seen,
            "fetched_at": k.fetched_at.isoformat(),
            "last_requested_at": k.last_requested_at.isoformat() if k.last_requested_at else None,
            "last_changed_at": k.last_changed_at.isoformat() if k.last_changed_at else None,
            "question": f"{k.req_method} {k.req_url}"[:200] if k.req_url else "",
            "versions": [{
                "version": version, "origin": origin, "size_bytes": size_bytes,
                "stored": "body" if has_body else ("ref" if body_of else "hash"),
                "fetched_at": fetched_at.isoformat(),
            } for version, origin, size_bytes, fetched_at, body_of, has_body in reversed(vers)],
        })
    events = (await db.execute(
        select(CallRecord).where(CallRecord.endpoint_id == endpoint_id)
        .options(defer(CallRecord.error_request), defer(CallRecord.error_response))
        .order_by(CallRecord.id.desc()).limit(30))).scalars().all()
    return {"endpoint_id": endpoint_id, "keys": out_keys,
            "events": [{
                "at": e.created_at.isoformat(), "cached": e.cached, "status": e.status_code,
                "charged_micro": e.cost_charged_micro,
            } for e in events]}


@app.get("/admin/archive/body")
async def admin_archive_body(
    key_hash: str, version: int,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """One authorized historical version through the common archive body reader."""
    from ..models import ArchiveKey as AK
    from ..models import ArchiveSnapshot as AS
    from .. import archive_bodies

    key = (await db.execute(select(AK).where(AK.key_hash == key_hash))).scalars().one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="no such key")
    snap = (await db.execute(select(AS).options(*archive_bodies.read_options("admin"))
                            .where(AS.key_id == key.id, AS.version == version)
                             )).scalars().one_or_none()
    if snap is None:
        raise HTTPException(status_code=404, detail="no such version")
    carrier = None
    if snap.body_of is not None:
        carrier = (await db.execute(select(AS.version).where(
            AS.id == snap.body_of, AS.key_id == key.id))).scalar_one_or_none()
    pointer = await archive_bodies.pointer(db, snap, "admin")
    await db.close()
    body = await archive_bodies.read(pointer, "admin")
    text = None
    if body is not None:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    return {"key_hash": key_hash, "version": snap.version, "origin": snap.origin,
            "fetched_at": snap.fetched_at.isoformat(), "media_type": snap.media_type,
            "size_bytes": snap.size_bytes, "stored": body is not None,
            "body_storage": snap.body_storage,
            "carried_by_version": carrier,
            "body_text": text,
            "note": None if body is not None else
            "body bytes are unavailable (policy, size, pruning or storage failure)"}


@app.get("/admin/archive/panel", response_class=HTMLResponse, include_in_schema=False)
async def admin_archive_panel() -> FileResponse:
    """The archive panel's page SHELL — deliberately unauthenticated, because it contains no data:
    every number on it arrives via fetch() to /admin/archive*, each of which requires the admin
    token the page asks the operator to paste (kept in the browser's localStorage)."""
    from pathlib import Path
    page = Path(__file__).parent.parent / "web" / "archive-panel.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="archive-panel.html not bundled")
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/admin/referrals")
async def admin_referrals(
    status: str = "", limit: int = Query(200, ge=1, le=1000),
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """Every referral, across every team — who invited whom, what it cost, and what is still owed.

    Cross-org, so `require_superadmin` and nothing weaker, exactly like the reconcile trio above.
    Read-only: it pays nothing and refuses nothing. `pending_payout_micro` is what the sweep will
    grant once holds elapse, and it is the number that says whether this program is affordable
    before the money leaves — the same job `provider_spend` does for provider bills.

    This is also the report the influencer tier will read when it lands: a cash payout run is this
    list, filtered to partners on a contract, exported.
    """
    s = get_settings()
    q = select(Referral).order_by(Referral.created_at.desc()).limit(limit)
    if status:
        q = q.where(Referral.status == status)
    rows = (await db.execute(q)).scalars().all()

    emails: dict[int, str] = {}
    for uid in {r.referrer_user_id for r in rows} | {r.referred_user_id for r in rows}:
        u = await db.get(User, uid)
        if u is not None:
            emails[uid] = u.email

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    pending = sum(int(s.referral_referrer_micro) + int(s.referral_referred_micro)
                  for r in rows if r.status == "qualified")
    paid = sum(r.referrer_reward_micro + r.referred_reward_micro for r in rows if r.status == "paid")
    return {
        "counts": by_status,
        "paid_micro": paid,
        "pending_payout_micro": pending,
        "referrals": [{
            "id": r.id, "code": r.code, "status": r.status, "reason": r.reject_reason,
            "referrer": emails.get(r.referrer_user_id, ""),
            "referred": emails.get(r.referred_user_id, ""),
            "referred_org_id": r.referred_org_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "qualified_at": r.qualified_at.isoformat() if r.qualified_at else None,
            "paid_at": r.paid_at.isoformat() if r.paid_at else None,
            "paid_micro": r.referrer_reward_micro + r.referred_reward_micro,
        } for r in rows],
    }


async def _is_last_active_superadmin(db: AsyncSession, target: User) -> bool:
    """True if `target` is currently the ONLY active (unsuspended) super-admin, so demoting /
    suspending / deleting them would leave the platform with no reachable admin."""
    if not (target.is_superadmin and not target.suspended):
        return False  # not an active super-admin → removing them changes nothing about the floor
    actives = (
        await db.execute(select(User).where(User.is_superadmin.is_(True), User.suspended.is_(False)))
    ).scalars().all()
    return len(actives) <= 1


class BoolIn(BaseModel):
    value: bool = True


app = APIRouter()
mutations_router = app


@app.post("/admin/users/{user_id}/superadmin")
async def admin_set_superadmin(
    user_id: int, body: BoolIn, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    # Demoting the last active super-admin locks everyone out of /admin/* (the env token bypasses).
    if not body.value and principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot demote the last active super-admin")
    user.is_superadmin = body.value
    await db.commit()
    return {"user_id": user_id, "is_superadmin": user.is_superadmin}


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(
    user_id: int, body: BoolIn, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if body.value and principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot suspend the last active super-admin")
    user.suspended = body.value
    await db.commit()
    return {"user_id": user_id, "suspended": user.suspended}


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot delete the last active super-admin")
    mem = (await db.execute(select(Membership).where(Membership.user_id == user_id))).scalars().all()
    affected = {m.org_id for m in mem}
    for m in mem:
        await delete_membership(db, m, actor_email=principal)
    await db.flush()
    emptied = []
    for oid in affected:
        survivors = (
            await db.execute(select(Membership).where(Membership.org_id == oid).order_by(Membership.id))
        ).scalars().all()
        if not survivors:  # an org left with zero members is dead — cascade it away
            org = await db.get(Org, oid)
            if org is not None:
                await cascade_delete_org(org, db)
                emptied.append(oid)
        elif not any(m.role == "owner" for m in survivors):
            # Deleting the sole owner would leave an ungovernable org (no one can pass _require_owner_of).
            # Promote the earliest-joined survivor so ownership never evaporates.
            survivors[0].role = "owner"
    # The USER row is about to go, so member-scoped rules must go from EVERY org — `DenyRule.user_id`
    # is a foreign key, and a surviving row would dangle (a hard error on Postgres).
    await drop_member_deny_rules(db, user_id)
    await db.delete(user)
    await db.commit()
    return {"deleted_user": user_id, "deleted_empty_orgs": emptied}


@app.post("/admin/orgs/{org_id}/suspend")
async def admin_suspend_org(
    org_id: int, body: BoolIn, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    org.suspended = body.value
    await db.commit()
    return {"org_id": org_id, "suspended": org.suspended}


@app.delete("/admin/orgs/{org_id}")
async def admin_delete_org(
    org_id: int, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    await cascade_delete_org(org, db)
    await db.commit()
    return {"deleted_org": org_id}


def _usd_to_micro(amount_usd: str | int | float) -> int:
    """USD string/number -> integer micro-USD. Uses Decimal to avoid float drift.

    Rejects amounts that cannot be exactly represented in micro-USD (finer than 6 decimal places),
    non-positive amounts, and non-numeric input.
    """
    try:
        value = Decimal(str(amount_usd))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail=f"amount_usd {amount_usd!r} is not a valid number")
    if value <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be positive")
    micro = value * 1_000_000
    if micro != micro.to_integral_value():
        raise HTTPException(
            status_code=400,
            detail=f"amount_usd {amount_usd} is finer than micro-USD (6 decimal places); it cannot be represented exactly"
        )
    return int(micro)


class CreditIn(BaseModel):
    amount_usd: str | int | float
    ref: str
    reason: str


@app.post("/admin/orgs/{org_id}/credit")
async def admin_credit_org(
    org_id: int, body: CreditIn, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session)
) -> dict:
    """Credit an org with promotional balance — the HTTP equivalent of scripts/manual_grant.py.

    The ONLY sanctioned way to credit balance through the API. Uses `money.grant()` under the hood,
    which writes block + balance + ledger in one transaction, preserving the invariant that
    `org.balance_micro == sum(block.remaining_micro) - sum(open hold.amount_micro)`.

    Why `promotional` and not a custom kind: spend burns blocks in `money._KIND_ORDER` order, and an
    unrecognized kind falls through to sort AFTER purchased credit. A comp should burn BEFORE the
    customer's own money (it's a marketing expense), which is what `promotional` already does.

    `ref` provides idempotency: a grant with the same ref for the same org is refused (409) rather than
    double-credited. This is a best-effort scan of `meta.ref` in existing ledger entries — the same
    sequential-reuse guard as scripts/manual_grant.py. It is NOT concurrency-safe: two simultaneous
    requests with the same ref can both pass the scan. For an HTTP endpoint called by one ops person
    at a time, that is acceptable; if it ever stops being true, the fix is a unique column.

    Returns the org_id, credited amount, new balance, block id, and ref — enough for ops to confirm.
    """
    micro = _usd_to_micro(body.amount_usd)
    ref = body.ref.strip()
    reason = body.reason.strip()
    if not ref:
        raise HTTPException(status_code=400, detail="ref is required (ops ticket / idempotency key)")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required (human explanation)")

    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")

    prior = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.org_id == org_id, LedgerEntry.kind == "grant")
    )).scalars().all()
    clash = next((e for e in prior if (e.meta or {}).get("ref") == ref), None)
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"ref {ref!r} was already used for a grant to org {org_id} "
                   f"(${clash.amount_micro / 1_000_000:.2f}, entry {clash.id}, {clash.created_at})"
        )

    block = await money.grant(
        db, org_id, amount_micro=micro, kind="promotional", once=False,
        meta={"ref": ref, "reason": reason, "source": "admin_credit_org", "principal": principal},
    )
    await db.commit()

    if block is None:
        raise HTTPException(status_code=500, detail="grant returned None unexpectedly")

    await db.refresh(org)
    return {
        "org_id": org_id,
        "amount_micro": micro,
        "amount_usd": float(Decimal(str(micro)) / 1_000_000),
        "balance_micro": org.balance_micro,
        "balance_usd": float(Decimal(str(org.balance_micro)) / 1_000_000),
        "block_id": block.id,
        "ref": ref,
    }
