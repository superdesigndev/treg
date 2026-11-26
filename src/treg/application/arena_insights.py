"""Incremental, database-backed Arena analytics. No upstream calls or request-path scans.

The collector is scheduled work, not web-process work: `treg-worker arena insights` runs
`drain()` on a cron, one process at a time. It used to be a coroutine started by every control/all
lifespan, which meant every web process (and every extra instance during a deploy) contending for
the same `ArenaInsightState` row lock and each walking `callrecord` on the database the money path
depends on. The web service keeps only `public_snapshot`, a primary-key read of the last published
aggregate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import statistics
import time
import zlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Integer, String, case, column, delete, func, literal, select, text, update, values
from sqlalchemy.exc import DBAPIError

from ..domain import arena, arena_insights as rules
from .. import archive_bodies
from ..domain import asynctasks
from ..domain.catalog import store
from ..infra.db import session_maker
from ..models import ArenaInsightState, ArenaObservation, ArchiveKey, ArchiveSnapshot, CallRecord
from ..timeutil import utcnow_naive as now

WINDOW_DAYS = 30
# Each refresh re-aggregates the whole 30-day window on the primary (tens of seconds on a large
# table, evicting the money path's cached pages). A 30-day leaderboard does not need minutes.
REFRESH_SECONDS = 1800
BATCH_SIZE = 100
MAX_BODY_BYTES = 2_000_000
# Statement timeout for the rewind query (seconds). The rewind catches recent evidence that arrived
# after the scan started; a timeout here skips the rewind this pass rather than blocking.
REWIND_TIMEOUT_S = 30
# Max retries for transient disconnects (ConnectionDoesNotExistError, connection reset).
MAX_TRANSIENT_RETRIES = 2
log = logging.getLogger(__name__)


def _is_transient_disconnect(exc: BaseException) -> bool:
    """True when `exc` looks like a transient connection drop (pg_terminate_backend, network reset).

    Covers ConnectionDoesNotExistError from asyncpg (connection closed mid-query, as when
    pg_terminate_backend terminates a backend), and InterfaceError wrapping connection resets.
    Does NOT include pool exhaustion or refused connections — those are capacity, not transient."""
    exc_str = str(exc).lower()
    if "connection was closed" in exc_str or "connectiondoesnotexisterror" in exc_str:
        return True
    if "connection reset" in exc_str or "broken pipe" in exc_str:
        return True
    if isinstance(exc, DBAPIError):
        orig = getattr(exc, "orig", None)
        if orig and _is_transient_disconnect(orig):
            return True
    return False


async def _rewind_cursor(db, endpoints: list[str], cutoff) -> int | None:
    """Find the minimum callrecord.id for recent Arena evidence, index-driven.

    Uses LATERAL to look up each endpoint individually, letting each probe ride
    ix_callrecord_endpoint_id_created_at (ORDER BY created_at, id LIMIT 1) instead of scanning
    the whole table with a large IN list. The global min is taken from those per-endpoint mins.

    Returns None if no recent records exist for any endpoint. On SQLite (tests), falls back to
    a simple query that works correctly on the small test dataset.
    """
    if not endpoints:
        return None

    if db.bind.dialect.name == "postgresql":
        # Build a VALUES CTE of endpoint_ids, then LATERAL join to get each endpoint's min id.
        # This ensures the planner uses the (endpoint_id, created_at) index for each lookup.
        # Query shape:
        #   WITH eps(eid) AS (VALUES ('ep1'), ('ep2'), ...)
        #   SELECT min(cr.id) FROM eps, LATERAL (
        #       SELECT id FROM callrecord
        #       WHERE endpoint_id = eps.eid AND created_at >= :cutoff
        #       ORDER BY created_at, id LIMIT 1
        #   ) cr
        endpoint_values = values(column("eid", String)).data([(e,) for e in endpoints]).cte("eps")
        lateral = (
            select(CallRecord.id)
            .where(CallRecord.endpoint_id == endpoint_values.c.eid, CallRecord.created_at >= cutoff)
            .order_by(CallRecord.created_at, CallRecord.id)
            .limit(1)
            .lateral("cr")
        )
        stmt = select(func.min(lateral.c.id)).select_from(endpoint_values.join(lateral, literal(True)))
        return (await db.execute(stmt)).scalar_one()
    else:
        # SQLite: simple query works fine on small test datasets
        return (await db.execute(
            select(func.min(CallRecord.id))
            .where(CallRecord.endpoint_id.in_(endpoints), CallRecord.created_at >= cutoff)
        )).scalar_one()


def _catalog():
    cat = store.load()
    endpoints = {eid: ep for eid, ep in cat.by_id.items()
                 if ep.get("capability") in arena.TASKS and ep.get("capability") not in arena.DISCOVERY_TASKS and eid in cat.adapters
                 and cat.adapters[eid].verified and cat.adapters[eid].route and ep.get("provider") not in {"treg", "wrangle"}
                 and eid not in arena.EXCLUDED and _first_answer_judged(ep) and ".bulk" not in eid}
    return cat, endpoints, _version()


def _version() -> str:
    return hashlib.sha256(json.dumps([rules.RULES_VERSION, arena.VERSION]).encode()).hexdigest()


def _first_answer_judged(ep: dict) -> bool:
    """Insights judge the archived first answer. An async row's first answer is a task id, unless
    the row declares that it usually answers at once (`terminal_on_submission`)."""
    return not ep.get("async") or ep["async"].get("terminal_on_submission") is True


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


async def _evidence(records, session_factory=session_maker):
    # Metadata and exact-version selection use a short session. Neither the cursor lock nor
    # another request-owned connection may survive across object reads, including DB fallback.
    async with session_factory() as db:
        keymap, pointers, _ = await _evidence_pointers(db, records)
    semaphore = asyncio.Semaphore(8)
    async def load(pair, pointer):
        async with semaphore:
            try:
                raw = await archive_bodies.read(pointer, "arena", session_factory=session_factory)
            except zlib.error:
                # Decompression now happens in the common reader. Preserve _decode's previous
                # per-record handling so one corrupt DB body cannot stop the entire collector.
                raw = None
            return pair, _decode(raw, None)
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(load(pair, pointer)) for pair, pointer in pointers.items()]
    bodies = dict(task.result() for task in tasks)
    result = {}
    for r in records:
        k = keymap.get(r.archive_key_hash)
        if k is None or k.endpoint_id != r.endpoint_id:
            continue
        query = {name: values[-1] for name, values in parse_qs(urlsplit(k.req_url).query, keep_blank_values=True).items()}
        body = _decode(k.req_body, None) if k.req_body else {}
        result[r.id] = (query, body or {}, bodies.get((k.id, r.archive_content_hash)))
    return result


async def _evidence_pointers(db, records, path="arena"):
    """The archive key of each record's question, and a body pointer and media type for the exact
    answer it received. `path` names the reader in the body-read switches and diagnostics."""
    keys = {r.archive_key_hash for r in records if r.archive_key_hash}
    if not keys:
        return {}, {}, {}
    # Read request metadata separately so explicit 404s can still be classified without a body.
    ak = (await db.execute(select(ArchiveKey).where(ArchiveKey.key_hash.in_(keys)))).scalars().all()
    keymap = {k.key_hash: k for k in ak}
    pairs = sorted({(keymap[r.archive_key_hash].id, r.archive_content_hash) for r in records
                    if r.archive_key_hash in keymap and r.archive_content_hash})
    # Only the newest carrier for each exact key/content pair; never substitute the latest answer.
    snaps = []
    if pairs:
        # Look up each request's newest matching version through the existing (key_id, version)
        # index. A large OR over key/content pairs can repeatedly scan the global content index
        # for common responses (e.g. identical verifier verdicts) before intersecting by key.
        wanted = values(column("key_id", Integer), column("content_hash", String)).data(pairs).cte("wanted")
        latest = (select(ArchiveSnapshot.id).where(ArchiveSnapshot.key_id == wanted.c.key_id,
            ArchiveSnapshot.content_hash == wanted.c.content_hash).order_by(ArchiveSnapshot.version.desc())
            .limit(1).correlate(wanted).scalar_subquery())
        snaps = (await db.execute(select(ArchiveSnapshot).options(*archive_bodies.read_options(path))
            .where(ArchiveSnapshot.id.in_(select(latest).select_from(wanted))))).scalars().all()
    pointers, media = {}, {}
    for snap in snaps:
        if snap.size_bytes > MAX_BODY_BYTES:
            continue
        try:
            pointers[snap.key_id, snap.content_hash] = await archive_bodies.pointer(db, snap, path)
        except zlib.error:
            # In DB mode the pointer loads/decompresses bytes before releasing this session.
            continue
        media[snap.key_id, snap.content_hash] = snap.media_type
    return keymap, pointers, media


async def _aggregate(db, version, until, endpoints):
    O = ArenaObservation
    where = (O.version == version, O.endpoint.in_(endpoints), O.created_at >= until - timedelta(days=WINDOW_DAYS),
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


async def collect_batch(session_factory=session_maker):
    """Read evidence outside transactions; validate the sampled cursor before publishing a batch.

    Runs inside the `treg-worker` process, whose only pool is the API one (nothing else shares it
    there); it is never awaited by a request handler.
    """
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
        cursor = state.cursor
        await db.commit()

    async with session_factory() as db:
        records = (await db.execute(select(CallRecord).where(CallRecord.id > cursor,
            CallRecord.created_at >= until - timedelta(days=WINDOW_DAYS), CallRecord.created_at < until,
            CallRecord.endpoint_id.in_(endpoints)).order_by(CallRecord.id).limit(BATCH_SIZE))).scalars().all()
    evidence = await _evidence(records, session_factory)

    async with session_factory() as db:
        state = (await db.execute(select(ArenaInsightState).where(
            ArenaInsightState.id == version).with_for_update())).scalar_one()
        # Another worker may have published while this batch fetched R2. Its cursor/cycle wins;
        # discard our speculative evidence instead of skipping records or overwriting the snapshot.
        if state.cursor != cursor or state.scan_until != until or state.updated_at is not None:
            return True
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
            if (category in ("hit", "miss") and ep.get("async")
                    and not asynctasks.finished_on_submission(ep["async"], evidence[record.id][2])):
                # "Still working" is no answer yet: neither a hit nor a miss.
                category = "unresolved_pending"
            obs_values = dict(id=record.id, version=version, endpoint=record.endpoint_id, task=ep["capability"],
                input=inp, request_hash=record.params_hash or "", category=category,
                duration_ms=record.duration_ms, created_at=record.created_at)
            observations.append(obs_values)
        if observations:
            stmt = _insert(db, ArenaObservation).values(observations)
            await db.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={k: getattr(stmt.excluded, k) for k in observations[0] if k != "id"}))
        if records:
            state.cursor = records[-1].id
        backlog = len(records) == BATCH_SIZE
        if not backlog:
            # Aggregate and rewind: run _aggregate under the state lock (it reads ArenaObservation),
            # then commit, then run rewind in a separate short session with statement_timeout.
            # This avoids holding FOR UPDATE across the potentially slow rewind query.
            agg_start = time.monotonic()
            state.payload = await _aggregate(db, version, until, endpoints)
            state.updated_at = current
            state.scan_until = current - timedelta(seconds=60)
            agg_ms = (time.monotonic() - agg_start) * 1000
            log.info("Arena aggregate completed in %.1f ms", agg_ms)
        db.add(state)
        await db.commit()

    # Rewind: find minimum id for recent evidence in a separate session.
    # This runs outside the state lock, with its own statement_timeout and retry.
    if not backlog:
        rewind_start = time.monotonic()
        rewind_cursor = None
        endpoint_list = list(endpoints.keys())
        cutoff = current - timedelta(seconds=60) - timedelta(minutes=10)

        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            try:
                async with session_factory() as db:
                    if db.bind.dialect.name == "postgresql":
                        await db.execute(text(f"SET LOCAL statement_timeout = '{REWIND_TIMEOUT_S}s'"))
                    rewind_cursor = await _rewind_cursor(db, endpoint_list, cutoff)
                break
            except DBAPIError as exc:
                if _is_transient_disconnect(exc) and attempt < MAX_TRANSIENT_RETRIES:
                    log.warning("Arena rewind transient disconnect (attempt %d/%d): %s",
                                attempt + 1, MAX_TRANSIENT_RETRIES + 1, exc)
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
                if sqlstate == "57014":  # query_canceled (statement_timeout)
                    log.warning("Arena rewind timed out after %ds; skipping rewind this pass", REWIND_TIMEOUT_S)
                    rewind_cursor = None
                    break
                raise
            except Exception as exc:
                if _is_transient_disconnect(exc) and attempt < MAX_TRANSIENT_RETRIES:
                    log.warning("Arena rewind transient disconnect (attempt %d/%d): %s",
                                attempt + 1, MAX_TRANSIENT_RETRIES + 1, exc)
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise

        rewind_ms = (time.monotonic() - rewind_start) * 1000
        if rewind_cursor is not None:
            async with session_factory() as db:
                state = (await db.execute(select(ArenaInsightState).where(
                    ArenaInsightState.id == version).with_for_update())).scalar_one()
                state.cursor = max(0, rewind_cursor - 1)
                db.add(state)
                await db.commit()
            log.info("Arena rewind completed in %.1f ms, cursor=%d", rewind_ms, rewind_cursor - 1)
        else:
            log.info("Arena rewind completed in %.1f ms, no rewind needed", rewind_ms)

    return backlog


async def prune(session_factory=session_maker) -> None:
    """Drop facts older than the window being collected and everything of superseded versions.

    Runs every pass, backlog or not: tying it to publication let the table grow without bound
    while a backfill was unfinished. A superseded state row is kept until the current version has
    published, because `public_snapshot` falls back to it until then; its cursor is reset with its
    facts, so a rolled-back collector rebuilds them instead of publishing over the payload.
    """
    _, _, version = _catalog()
    O = ArenaObservation
    async with session_factory() as db:
        state = await db.get(ArenaInsightState, version)
        until = min(state.scan_until, now()) if state else now()
        await db.execute(delete(O).where(O.version == version, O.created_at < until - timedelta(days=WINDOW_DAYS)))
        stale = (await db.execute(select(ArenaInsightState.id).where(ArenaInsightState.id != version))).scalars().all()
        if stale:
            await db.execute(delete(O).where(O.version.in_(stale)))
            await db.execute(update(ArenaInsightState).where(ArenaInsightState.id.in_(stale),
                (ArenaInsightState.cursor != 0) | ArenaInsightState.updated_at.is_not(None))
                .values(cursor=0, updated_at=None, scan_until=now() - timedelta(seconds=60)))
            if state and state.payload.get("status") == "ready":
                await db.execute(delete(ArenaInsightState).where(ArenaInsightState.id.in_(stale)))
        await db.commit()


async def drain(session_factory=session_maker, *, max_seconds: float = 110.0) -> dict:
    """One scheduled pass: collect batches until the backlog is empty or the time budget is spent.

    Sized for a two-minute cron: a pass that still has backlog at `max_seconds` stops and the next
    run resumes from the persisted cursor. The `REFRESH_SECONDS` gate inside `collect_batch` makes
    an early re-run a no-op, so overlapping schedules cannot double-collect. A failed batch is
    logged and ends the pass; the last published aggregate stays in place.
    """
    started = time.monotonic()
    batches = 0
    try:
        await prune(session_factory)
    except Exception:
        log.exception("Arena observation prune failed; collecting anyway")
    while True:
        try:
            backlog = await collect_batch(session_factory)
        except Exception:
            log.exception("Arena observation refresh failed; retaining the last database aggregate")
            return {"batches": batches, "backlog": True, "failed": True}
        batches += 1
        if not backlog or time.monotonic() - started >= max_seconds:
            return {"batches": batches, "backlog": backlog, "failed": False}
