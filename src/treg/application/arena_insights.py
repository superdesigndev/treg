"""Incremental, database-backed Arena analytics. No upstream calls or request-path scans."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import statistics
import zlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Integer, String, case, column, delete, func, select, values
from sqlalchemy.orm import aliased

from ..domain import arena, arena_insights as rules
from ..domain.catalog import store
from ..infra.db import background_session_maker, session_maker
from ..models import ArenaInsightState, ArenaObservation, ArchiveKey, ArchiveSnapshot, CallRecord
from ..timeutil import utcnow_naive as now

WINDOW_DAYS = 30
REFRESH_SECONDS = 120
BATCH_SIZE = 100
MAX_BODY_BYTES = 2_000_000
log = logging.getLogger(__name__)


def _catalog():
    cat = store.load()
    endpoints = {eid: ep for eid, ep in cat.by_id.items()
                 if ep.get("capability") in arena.TASKS and ep.get("capability") not in arena.DISCOVERY_TASKS and eid in cat.adapters
                 and cat.adapters[eid].verified and ep.get("provider") not in {"treg", "wrangle"}
                 and eid not in arena.EXCLUDED and not ep.get("async") and ".bulk" not in eid}
    fingerprint = {eid: {"path": ep.get("path"), "method": ep.get("method"),
                        "adapter": cat.adapters[eid].__dict__, "contract": cat.contracts[ep["capability"]].__dict__}
                   for eid, ep in sorted(endpoints.items())}
    version = hashlib.sha256(json.dumps([rules.RULES_VERSION, arena.VERSION, fingerprint], sort_keys=True, default=str).encode()).hexdigest()
    return cat, endpoints, version


def _insert(db, model):
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


def _empty(at):
    return {"version": 2, "status": "warming", "since": (at - timedelta(days=WINDOW_DAYS)).isoformat() + "Z",
            "until": at.isoformat() + "Z", "updated_at": None, "refresh_seconds": REFRESH_SECONDS, "rows": []}


async def public_snapshot(session_factory=session_maker):
    # Read only saved snapshots; never scan raw audit evidence on a page request.
    from .arena_verification_insights import public_snapshot as verification_snapshot
    _, _, version = _catalog()
    async with session_factory() as db:
        state = await db.get(ArenaInsightState, version)
        saved = state.payload if state else None
        if not saved:
            # A new catalog version must not blank the page during its first pass.
            # The cursor's updated_at is cleared on refresh, so order by the
            # publication timestamp inside the retained payload instead.
            snapshot = ArenaInsightState.payload
            saved = (await db.execute(select(snapshot).where(
                snapshot["version"].as_integer() == 2,
                snapshot["status"].as_string() == "ready",
                snapshot["updated_at"].as_string().is_not(None),
            ).order_by(snapshot["updated_at"].as_string().desc(), ArenaInsightState.id)
                .limit(1))).scalar_one_or_none()
        payload = dict(saved) if saved else _empty(now())
        payload["verification"] = await verification_snapshot(db)
        return payload


def _decode(raw, enc):
    if raw is None:
        return None
    try:
        if enc == "zlib":
            decoder = zlib.decompressobj()
            raw = decoder.decompress(raw, MAX_BODY_BYTES + 1)
            if not decoder.eof or decoder.unconsumed_tail:
                return None
        if len(raw) > MAX_BODY_BYTES:
            return None
        return json.loads(raw)
    except (ValueError, UnicodeError, zlib.error, TypeError):
        return None


async def _evidence(db, records):
    keys = {r.archive_key_hash for r in records if r.archive_key_hash}
    if not keys:
        return {}
    # Read request metadata separately so explicit 404s can still be classified without a body.
    ak = (await db.execute(select(ArchiveKey).where(ArchiveKey.key_hash.in_(keys)))).scalars().all()
    keymap = {k.key_hash: k for k in ak}
    pairs = sorted({(keymap[r.archive_key_hash].id, r.archive_content_hash) for r in records
                    if r.archive_key_hash in keymap and r.archive_content_hash})
    # Only the newest carrier for each exact key/content pair; never substitute the latest answer.
    carrier = aliased(ArchiveSnapshot)
    snaps = []
    if pairs:
        # Look up each request's newest matching version through the existing (key_id, version)
        # index. A large OR over key/content pairs can repeatedly scan the global content index
        # for common responses (e.g. identical verifier verdicts) before intersecting by key.
        wanted = values(column("key_id", Integer), column("content_hash", String)).data(pairs).cte("wanted")
        latest = (select(ArchiveSnapshot.id).where(ArchiveSnapshot.key_id == wanted.c.key_id,
            ArchiveSnapshot.content_hash == wanted.c.content_hash).order_by(ArchiveSnapshot.version.desc())
            .limit(1).correlate(wanted).scalar_subquery())
        snaps = (await db.execute(select(ArchiveSnapshot, carrier.body, carrier.enc)
            .outerjoin(carrier, carrier.id == ArchiveSnapshot.body_of)
            .where(ArchiveSnapshot.id.in_(select(latest).select_from(wanted))))).all()
    bodies = {(s.key_id, s.content_hash): _decode(s.body if s.body is not None else body,
               s.enc if s.body is not None else enc) for s, body, enc in snaps}
    result = {}
    for r in records:
        k = keymap.get(r.archive_key_hash)
        if k is None or k.endpoint_id != r.endpoint_id:
            continue
        query = {name: values[-1] for name, values in parse_qs(urlsplit(k.req_url).query, keep_blank_values=True).items()}
        body = _decode(k.req_body, None) if k.req_body else {}
        result[r.id] = (query, body or {}, bodies.get((k.id, r.archive_content_hash)))
    return result


async def _aggregate(db, version, until):
    O = ArenaObservation
    where = (O.version == version, O.created_at >= until - timedelta(days=WINDOW_DAYS),
             O.created_at < until, O.input != "unknown")
    columns = (O.task, O.input, O.endpoint)
    counts = (await db.execute(select(*columns, O.category, func.count()).where(*where)
                              .group_by(*columns, O.category))).all()
    grouped = {}
    for task, inp, endpoint, category, count in counts:
        row = grouped.setdefault((task, inp, endpoint), {"task": task, "input": inp, "endpoint": endpoint,
            "provider": endpoint.split(".")[0], "hits": 0, "misses": 0, "unique_requests": 0,
            "unique_rate": None, "median_hit_ms": None, "timed_hits": 0, "excluded": 0, "unresolved": 0})
        if category in ("hit", "miss"):
            row["hits" if category == "hit" else "misses"] += count
        elif category.startswith("excluded_"):
            row["excluded"] += count
        else:
            row["unresolved"] += count
    # A failed retry must not erase an earlier evaluable result. Deduplicate only decided calls.
    latest = select(*columns, O.category, func.row_number().over(
        partition_by=(O.endpoint, O.input, O.request_hash), order_by=(O.created_at.desc(), O.id.desc())).label("n")
        ).where(*where, O.category.in_(["hit", "miss"]), O.request_hash != "").subquery()
    unique = (await db.execute(select(latest.c.task, latest.c.input, latest.c.endpoint, func.count(),
        func.sum(case((latest.c.category == "hit", 1), else_=0))).where(latest.c.n == 1)
        .group_by(latest.c.task, latest.c.input, latest.c.endpoint))).all()
    for task, inp, endpoint, total, hits in unique:
        grouped[(task, inp, endpoint)].update(unique_requests=total, unique_rate=round(100 * hits / total, 2))
    timed = (*where, O.category == "hit", O.duration_ms.is_not(None), O.duration_ms >= 0)
    if db.bind.dialect.name == "postgresql":
        durations = (await db.execute(select(*columns, func.count(),
            func.percentile_cont(0.5).within_group(O.duration_ms)).where(*timed).group_by(*columns))).all()
    else:
        # SQLite has no percentile_cont. Local/test databases use the same exact median in Python.
        values = {}
        for task, inp, endpoint, ms in (await db.execute(select(*columns, O.duration_ms).where(*timed))).all():
            values.setdefault((task, inp, endpoint), []).append(ms)
        durations = [(*k, len(v), statistics.median(v)) for k, v in values.items()]
    for task, inp, endpoint, count, median in durations:
        grouped[(task, inp, endpoint)].update(timed_hits=count, median_hit_ms=median)
    first, last = (await db.execute(select(func.min(O.created_at), func.max(O.created_at))
                                   .where(*where))).one()
    return {**_empty(until), "status": "ready", "updated_at": now().isoformat() + "Z",
            "observed_since": first.isoformat() + "Z" if first else None,
            "observed_until": last.isoformat() + "Z" if last else None,
            "rows": sorted(grouped.values(), key=lambda r: (r["task"], r["input"], r["endpoint"]))}


async def collect_batch(session_factory=background_session_maker):
    """One bounded transaction; serialized cursor across control workers. Returns True on backlog."""
    cat, endpoints, version = _catalog()
    current = now()
    async with session_factory() as db:
        stmt = _insert(db, ArenaInsightState).values(id=version, cursor=0,
            scan_until=current - timedelta(seconds=60), updated_at=None, payload={})
        await db.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
        state = (await db.execute(select(ArenaInsightState).where(ArenaInsightState.id == version).with_for_update())).scalar_one()
        if state.updated_at:
            if (current - state.updated_at).total_seconds() < REFRESH_SECONDS:
                await db.commit()
                return False
            # Freeze a fresh cutoff for this cycle, while keeping the prior public payload.
            state.scan_until = current - timedelta(seconds=60)
            state.updated_at = None
        until = state.scan_until
        records = (await db.execute(select(CallRecord).where(CallRecord.id > state.cursor,
            CallRecord.created_at >= until - timedelta(days=WINDOW_DAYS), CallRecord.created_at < until,
            CallRecord.endpoint_id.in_(endpoints)).order_by(CallRecord.id).limit(BATCH_SIZE))).scalars().all()
        evidence = await _evidence(db, records)
        observations = []
        for record in records:
            ep = endpoints[record.endpoint_id]
            row = {name: getattr(record, name) for name in ("kind", "cached", "credential_tier", "refused_by",
                   "error_request", "error_response", "status_code", "path", "method", "endpoint_id", "params_hash")}
            try:
                inp, category = rules.classify_record(row, ep, cat.adapters[record.endpoint_id],
                                                    cat.contracts[ep["capability"]], evidence.get(record.id))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                # A malformed archived request/response must not pin the cursor forever.
                inp, category = "unknown", "unresolved_evidence"
            if category in ("hit", "miss") and not record.params_hash:
                category = "unresolved_identity"
            values = dict(id=record.id, version=version, endpoint=record.endpoint_id, task=ep["capability"],
                input=inp, request_hash=record.params_hash or "", category=category,
                duration_ms=record.duration_ms, created_at=record.created_at)
            observations.append(values)
        if observations:
            stmt = _insert(db, ArenaObservation).values(observations)
            await db.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={k: getattr(stmt.excluded, k) for k in observations[0] if k != "id"}))
        if records:
            state.cursor = records[-1].id
        backlog = len(records) == BATCH_SIZE
        if not backlog:
            state.payload = await _aggregate(db, version, until)
            state.updated_at = current
            # Revisit recent evidence for delayed archive writes / concurrent audit commits.
            rewind = (await db.execute(select(func.min(CallRecord.id)).where(
                CallRecord.created_at >= until - timedelta(minutes=10)))).scalar_one()
            state.cursor = max(0, rewind - 1) if rewind else state.cursor
            state.scan_until = current - timedelta(seconds=60)
            await db.execute(delete(ArenaObservation).where(ArenaObservation.created_at < until - timedelta(days=WINDOW_DAYS)))
        db.add(state)
        await db.commit()
    return backlog


async def worker():
    while True:
        try:
            backlog = await collect_batch()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Arena observation refresh failed; retaining the last database aggregate")
            backlog = False
        await asyncio.sleep(0.25 if backlog else REFRESH_SECONDS)
