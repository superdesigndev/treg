"""Fold `callrecord` into per-endpoint, per-day reliability buckets (`EndpointDayStat`).

Why this exists: the catalog's "does it still work, how fast" numbers were computed on demand by
`domain.catalog.stats.observed`, a thirty-day aggregate over the audit table run by every web
process whenever its cache expired or started cold. The `treg-worker catalog stats` cron consumes
that evidence incrementally, and cache refreshes read only bounded UTC day buckets per endpoint.

How it walks the table: by primary key from a persisted cursor, never by `created_at` (the audit
table has no bare `created_at` index, and does not need one for this). Rows younger than `LAG`
are left for the next run: an audit insert commits a few seconds after its `created_at`, and the
ids are handed out at insert, so a row that is still uncommitted when a run passes its id is
always younger than the lag and everything from the first young row onward is deferred. Audit is
lossy by design (`audit.py` sheds rows under load), so the one remaining race, a row queued for
longer than the lag before its insert, only ever undercounts a day by a row it already might have
lost. The first run starts at the id of the first row inside the window, found by bisection on
the primary key so the backfill never scans a row older than the window. Readers use only complete,
recent folds; an unavailable worker never authorizes a live audit-table aggregate.

Late terminal corrections are coalesced in `EndpointStatDirty`. Each repair replaces one UTC day
bucket, under the same cursor lock as the append fold, and includes only already-folded ids.
Old mutable buckets are seeded for repair before readers trust their terminal evidence. Repairs
have both per-bucket and per-pass budgets; a failed bucket remains queued with retry backoff.

This module is the only runtime writer of `endpointdaystat` and `endpointstatcursor`. Audit marks
dirty buckets; this worker acknowledges them after replacement, in the same transaction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from sqlalchemy import and_, delete, func, select, tuple_, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.catalog import stats
from ..domain.catalog import store as catalog_store
from ..infra import catalog_stat_dirty
from ..infra.db import session_maker
from ..models import CallRecord, EndpointDayStat, EndpointStatCursor, EndpointStatDirty
from ..timeutil import utcnow_naive

CURSOR_ID = "callrecord"
LAG = timedelta(seconds=60)      # same allowance the Arena collector gives audit commits
BATCH_ROWS = 5_000
MAX_DIRTY_BUCKETS = 20
MAX_REPAIR_SECONDS = 60.0
BUCKET_TIMEOUT_S = 10.0
REPAIR_RETRY_S = 60
log = logging.getLogger("treg.catalog")


def _insert(db: AsyncSession):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def _day(at: datetime) -> str:
    return at.strftime("%Y-%m-%d")


def _tally_of(row: EndpointDayStat) -> stats.Tally:
    return stats.Tally(n=row.n, ok=row.ok, bad=row.bad, last_ok=row.last_ok_at, hits=row.hits,
                       hit_decided=row.hit_decided, paid_hits=row.paid_hits,
                       free_misses=row.free_misses, latency_seen=row.latency_seen,
                       latencies=list(row.latency_sample or []), verdicts=dict(row.verdicts or {}))


async def _store_tally(db: AsyncSession, endpoint_id: str, day: str, tally: stats.Tally,
                       at: datetime, *, terminal_reconciled: bool) -> None:
    values = dict(endpoint_id=endpoint_id, day=day, n=tally.n, ok=tally.ok, bad=tally.bad,
                  last_ok_at=tally.last_ok, hits=tally.hits, hit_decided=tally.hit_decided,
                  paid_hits=tally.paid_hits, free_misses=tally.free_misses,
                  latency_seen=tally.latency_seen, latency_sample=tally.latencies,
                  verdicts=tally.verdicts, updated_at=at,
                  terminal_reconciled=terminal_reconciled)
    stmt = _insert(db)(EndpointDayStat).values(**values)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=["endpoint_id", "day"],
        set_={k: getattr(stmt.excluded, k) for k in values if k not in ("endpoint_id", "day")}))


async def _seed_corrections(session_factory, at: datetime) -> None:
    """Queue legacy mutable buckets without scanning CallRecord or re-marking existing work."""
    cat = catalog_store.load()
    mutable = [eid for eid, ep in cat.by_id.items() if ep.get("async") and eid in cat.adapters]
    async with session_factory() as db:
        state = (await db.execute(select(EndpointStatCursor).where(
            EndpointStatCursor.id == CURSOR_ID).with_for_update())).scalar_one_or_none()
        if state is None:
            return
        await catalog_stat_dirty.prune(db, stats.window_days(at))
        await catalog_stat_dirty.seed_legacy(db, mutable, stats.window_days(at), at)
        await db.commit()


async def requeue_async_stats(session_factory=session_maker, *, apply: bool = False,
                              now: datetime | None = None) -> dict:
    """Preview or explicitly requeue existing mutable async buckets in the current window.

    Reset readiness and mark the selected days atomically under the fold cursor lock. A later
    False-to-True transition then proves a full repair, even when its pass began before this
    command. Normal append folding preserves False. This command never reads source audit rows.
    """
    at = now or utcnow_naive()
    since, today = stats.window_days(at), _day(at)
    cat = catalog_store.load()
    mutable = [eid for eid, ep in cat.by_id.items() if ep.get("async") and eid in cat.adapters]
    async with session_factory() as db:
        if apply:
            state = (await db.execute(select(EndpointStatCursor).where(
                EndpointStatCursor.id == CURSOR_ID).with_for_update())).scalar_one_or_none()
            if state is None:
                raise ValueError("catalog stats cursor missing; run catalog stats before requeueing")
        keys = list((await db.execute(select(EndpointDayStat.endpoint_id, EndpointDayStat.day)
            .where(EndpointDayStat.endpoint_id.in_(mutable), EndpointDayStat.day >= since,
                   EndpointDayStat.day <= today)
            .order_by(EndpointDayStat.endpoint_id, EndpointDayStat.day))).tuples())
        if apply and keys:
            await db.execute(update(EndpointDayStat).where(
                tuple_(EndpointDayStat.endpoint_id, EndpointDayStat.day).in_(keys)
            ).values(terminal_reconciled=False))
            await catalog_stat_dirty.mark_dirty(db, keys, now=at)
            await db.commit()
    return {"kind": "catalog-async-repair", "version": 1, "applied": apply,
            "requested_at": at.isoformat(), "window_start": since, "window_end": today,
            "target_count": len(keys),
            "targets": [{"endpoint_id": endpoint, "day": day} for endpoint, day in keys]}


def _repair_manifest_keys(manifest: dict) -> list[tuple[str, str]]:
    """Reject previews or malformed target lists before opening a database session."""
    if (not isinstance(manifest, dict) or manifest.get("kind") != "catalog-async-repair"
            or type(manifest.get("version")) is not int or manifest["version"] != 1
            or manifest.get("applied") is not True
            or not isinstance(manifest.get("targets"), list)):
        raise ValueError("repair status requires an applied catalog-async-repair manifest")
    requested_at = manifest.get("requested_at")
    start, end = manifest.get("window_start"), manifest.get("window_end")
    if not all(isinstance(value, str) for value in (requested_at, start, end)):
        raise ValueError("invalid repair manifest window or timestamp")
    try:
        datetime.fromisoformat(requested_at)
        if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end:
            raise ValueError("repair manifest window must use YYYY-MM-DD")
    except ValueError as exc:
        raise ValueError("invalid repair manifest window or timestamp") from exc
    if start > end:
        raise ValueError("repair manifest window is reversed")
    keys = []
    for target in manifest["targets"]:
        if not isinstance(target, dict):
            raise ValueError("invalid repair manifest target")
        endpoint, day = target.get("endpoint_id"), target.get("day")
        if not isinstance(endpoint, str) or not endpoint or not isinstance(day, str):
            raise ValueError("invalid repair manifest target")
        try:
            canonical = date.fromisoformat(day).isoformat()
        except ValueError as exc:
            raise ValueError("invalid repair manifest day") from exc
        if canonical != day:
            raise ValueError("repair manifest days must use YYYY-MM-DD")
        if not start <= day <= end:
            raise ValueError("repair manifest target is outside its window")
        keys.append((endpoint, day))
    if (type(manifest.get("target_count")) is not int
            or len(set(keys)) != len(keys) or manifest["target_count"] != len(keys)):
        raise ValueError("repair manifest target count or uniqueness mismatch")
    return sorted(keys)


async def repair_status(manifest: dict, session_factory=session_maker, *,
                        now: datetime | None = None) -> dict:
    """Check this applied manifest's coverage, not the readiness of a deployment or its writers.

    Read each bucket's flag and dirty marker in one statement snapshot. A missing bucket, an
    unreconciled bucket or another queued correction remains incomplete. Targets that have left
    the reader's window are reported separately rather than claimed as successfully repaired.
    """
    keys = _repair_manifest_keys(manifest)
    since = stats.window_days(now or utcnow_naive())
    current = [key for key in keys if key[1] >= since]
    observed = {}
    if current:
        async with session_factory() as db:
            rows = (await db.execute(select(
                EndpointDayStat.endpoint_id, EndpointDayStat.day,
                EndpointDayStat.terminal_reconciled,
                EndpointStatDirty.endpoint_id.label("dirty_endpoint"),
            ).outerjoin(EndpointStatDirty, and_(
                EndpointDayStat.endpoint_id == EndpointStatDirty.endpoint_id,
                EndpointDayStat.day == EndpointStatDirty.day,
            )).where(tuple_(EndpointDayStat.endpoint_id, EndpointDayStat.day).in_(current)))).all()
        observed = {(row.endpoint_id, row.day): row for row in rows}
    counts = dict(repaired=0, pending=0, missing=0, aged_out=0)
    targets = []
    for endpoint, day in keys:
        row = observed.get((endpoint, day))
        if day < since:
            status = "aged_out"
        elif row is None:
            status = "missing"
        elif not row.terminal_reconciled or row.dirty_endpoint is not None:
            status = "pending"
        else:
            status = "repaired"
        counts[status] += 1
        targets.append({"endpoint_id": endpoint, "day": day, "status": status})
    return {"complete": not (counts["pending"] or counts["missing"]), **counts,
            "targets": targets}


async def _repair_bucket(session_factory, endpoint_id: str, day: str, at: datetime) -> bool:
    """Replace exactly the folded portion of a day; concurrent corrections keep their marker."""
    async with session_factory() as db:
        state = (await db.execute(select(EndpointStatCursor).where(
            EndpointStatCursor.id == CURSOR_ID).with_for_update())).scalar_one_or_none()
        if state is None:
            return False
        marker = await db.get(EndpointStatDirty, (endpoint_id, day))
        if marker is None or marker.retry_at > at:
            return False
        revision = marker.revision
        start = datetime.fromisoformat(day)
        tally = stats.Tally()
        empty_fails = catalog_store.empty_is_failure(endpoint_id)
        # The endpoint/time pair bounds the existing index. Do not lock source rows: audit owns
        # their corrections and must not wait for this scan. Streaming bounds Python memory.
        # Bound retained correction age by this source snapshot, not the earlier pass timestamp.
        scanned_at = utcnow_naive()
        rows = await db.stream(select(
            CallRecord.status_code, CallRecord.created_at, CallRecord.duration_ms,
            CallRecord.hit, CallRecord.cost_observed_micro, CallRecord.verdict,
        ).where(CallRecord.endpoint_id == endpoint_id,
                CallRecord.created_at >= start, CallRecord.created_at < start + timedelta(days=1),
                CallRecord.id <= state.cursor_id,
                CallRecord.refused_by.is_(None)).execution_options(yield_per=BATCH_ROWS))
        try:
            # Cross the async result boundary once per bounded batch, then unpack rows locally.
            # The query still reads the complete folded day from one source snapshot.
            async for batch in rows.partitions(BATCH_ROWS):
                for status_code, created_at, duration_ms, hit, cost, verdict in batch:
                    tally.fold(status_code=status_code, created_at=created_at,
                               duration_ms=duration_ms, hit=hit, cost_observed_micro=cost,
                               refused_by=None, verdict=verdict, empty_fails=empty_fails)
                # Buffered rows need no I/O await; let timeout/cancellation run between batches.
                await asyncio.sleep(0)
        finally:
            await rows.close()
        await _store_tally(db, endpoint_id, day, tally, at, terminal_reconciled=True)
        # UPDATE + mark is one audit transaction. A marker changed while the source scan ran is
        # retained; an upsert waiting on this DELETE recreates it after our commit.
        await catalog_stat_dirty.acknowledge(db, endpoint_id, day, revision, scanned_at=scanned_at)
        await db.commit()
    return True


async def _repair_dirty(session_factory, at: datetime, *, max_dirty_buckets: int,
                        max_repair_seconds: float, bucket_timeout_s: float) -> dict:
    started = time.monotonic()
    repaired = failed = 0
    async with session_factory() as db:
        # Read only a bounded batch from the retry index. Do not hold marker row locks across a
        # bucket scan: audit must remain able to advance its revision.
        pending = (await db.execute(select(EndpointStatDirty.endpoint_id, EndpointStatDirty.day)
            .where(EndpointStatDirty.retry_at <= at, EndpointStatDirty.day >= stats.window_days(at))
            .order_by(EndpointStatDirty.retry_at, EndpointStatDirty.endpoint_id, EndpointStatDirty.day)
            .limit(max_dirty_buckets))).all() if max_dirty_buckets and max_repair_seconds > 0 else []
    for endpoint_id, day in pending:
        remaining = max_repair_seconds - (time.monotonic() - started)
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(min(bucket_timeout_s, remaining)):
                repaired += int(await _repair_bucket(session_factory, endpoint_id, day, at))
        except (TimeoutError, DBAPIError):
            failed += 1
            log.warning("catalog day repair deferred: endpoint=%s day=%s", endpoint_id, day, exc_info=True)
            # The failed replacement rolled back, including its marker acknowledgement. Updating
            # only retry_at preserves a concurrently incremented revision and its original age.
            async with session_factory() as db:
                await catalog_stat_dirty.defer(
                    db, endpoint_id, day, at + timedelta(seconds=REPAIR_RETRY_S))
                await db.commit()
    async with session_factory() as db:
        count = (await db.execute(select(func.count()).select_from(EndpointStatDirty).where(
            EndpointStatDirty.day >= stats.window_days(at)))).scalar_one()
    return {"repaired": repaired, "repair_failed": failed, "repair_pending": int(count)}


async def _first_id_at(db: AsyncSession, since: datetime) -> int:
    """The smallest `callrecord.id` whose `created_at` is at or after `since`, by bisection on the
    primary key (ids are handed out in insert order, which is time order to within the lag). About
    twenty-five primary-key lookups instead of a table walk. Gaps in the sequence are stepped over
    with a bounded `id >= mid` probe."""
    top = (await db.execute(select(func.max(CallRecord.id)))).scalar_one()
    if top is None:
        return 1
    lo, hi = 1, int(top) + 1    # every id below `lo` is older than `since`; `hi` is past the end
    best = int(top) + 1         # the smallest id seen so far that is inside the window
    while lo < hi:
        mid = (lo + hi) // 2
        probe = (await db.execute(
            select(CallRecord.id, CallRecord.created_at).where(CallRecord.id >= mid)
            .order_by(CallRecord.id).limit(1))).first()
        if probe is None:       # nothing at or above mid
            hi = mid
            continue
        row_id, created_at = int(probe[0]), probe[1]
        if created_at >= since:
            # `row_id` is inside the window and nothing exists in [mid, row_id), so the answer is
            # either below `mid` or exactly `row_id`.
            best = min(best, row_id)
            hi = mid
        else:
            lo = row_id + 1
    return best


async def refresh(session_factory=session_maker, *, max_rows: int = 500_000,
                  batch_rows: int = BATCH_ROWS, now: datetime | None = None,
                  max_dirty_buckets: int = MAX_DIRTY_BUCKETS,
                  max_repair_seconds: float = MAX_REPAIR_SECONDS,
                  bucket_timeout_s: float = BUCKET_TIMEOUT_S) -> dict:
    """One append pass followed by bounded late-terminal repairs.

    Each batch is its own short transaction: lock the cursor row, read the next `batch_rows` audit
    rows by id, read the current value of every day bucket they touch, fold, upsert those buckets,
    advance the cursor, commit. Nothing about a bucket is carried from one batch to the next: the
    row lock serializes overlapping runs (a slow backfill still running when the next schedule
    fires), and a bucket value remembered from an earlier batch would be stale by the time the lock
    is held again, so the later upsert would erase what the other run folded in between, with the
    cursor already past those rows and nothing to recover them from.

    Corrections seed legacy mutable buckets and replace marked days after the append pass.
    Their count, elapsed-time and per-bucket budgets do not bound append folding or queue
    maintenance. Readers require reconciled terminal evidence before publishing async summaries.
    """
    if max_dirty_buckets < 0 or max_repair_seconds < 0 or bucket_timeout_s <= 0:
        raise ValueError("catalog repair budgets must be nonnegative, with a positive bucket timeout")
    at = now or utcnow_naive()
    young = at - LAG
    since = at - timedelta(days=stats.WINDOW_DAYS)
    consumed = 0
    buckets_touched: set[tuple[str, str]] = set()
    caught_up = False
    cursor_id = 0
    while consumed < max_rows and not caught_up:
        async with session_factory() as db:
            state = (await db.execute(
                select(EndpointStatCursor).where(EndpointStatCursor.id == CURSOR_ID)
                .with_for_update())).scalar_one_or_none()
            if state is None:
                state = EndpointStatCursor(id=CURSOR_ID, cursor_id=await _first_id_at(db, since) - 1)
                db.add(state)
            limit = min(batch_rows, max_rows - consumed)
            rows = (await db.execute(
                select(CallRecord.id, CallRecord.endpoint_id, CallRecord.status_code,
                       CallRecord.created_at, CallRecord.duration_ms, CallRecord.hit,
                       CallRecord.cost_observed_micro, CallRecord.refused_by, CallRecord.verdict)
                .where(CallRecord.id > state.cursor_id).order_by(CallRecord.id).limit(limit)
            )).all()
            touched: dict[tuple[str, str], stats.Tally] = {}   # this batch only, read under the lock
            reconciled: dict[tuple[str, str], bool] = {}
            batch_touched: set[tuple[str, str]] = set()
            last: tuple[int, datetime] | None = None
            deferred = False
            for row_id, endpoint_id, status_code, created_at, duration_ms, hit, cost, refused_by, verdict in rows:
                if created_at >= young:
                    deferred = True     # still inside the commit lag: this row and everything after it wait
                    break
                last = (int(row_id), created_at)
                consumed += 1
                if not endpoint_id or created_at < since:
                    continue            # a plain tool call, or older than the window (the first run's bisect edge)
                key = (endpoint_id, _day(created_at))
                tally = touched.get(key)
                if tally is None:
                    existing = await db.get(EndpointDayStat, key)
                    tally = _tally_of(existing) if existing is not None else stats.Tally()
                    touched[key] = tally
                    reconciled[key] = existing is None or existing.terminal_reconciled
                if tally.fold(status_code=status_code, created_at=created_at, duration_ms=duration_ms,
                              hit=hit, cost_observed_micro=cost, refused_by=refused_by,
                              verdict=verdict, empty_fails=catalog_store.empty_is_failure(endpoint_id)):
                    batch_touched.add(key)
            buckets_touched |= batch_touched
            for endpoint_id, day in sorted(batch_touched):
                t = touched[(endpoint_id, day)]
                await _store_tally(db, endpoint_id, day, t, at,
                                   terminal_reconciled=reconciled[(endpoint_id, day)])
            if last is not None:
                state.cursor_id, state.watermark = last
            caught_up = deferred or len(rows) < limit
            if caught_up:
                state.caught_up_at = at
                # The window prune: a bucket older than the window (plus the day the window's
                # instant falls in) can never be read again.
                await db.execute(delete(EndpointDayStat).where(EndpointDayStat.day < stats.window_days(at)))
            state.updated_at = at
            db.add(state)
            cursor_id = state.cursor_id
            await db.commit()
            if not rows:
                break
    await _seed_corrections(session_factory, at)
    repairs = await _repair_dirty(session_factory, at, max_dirty_buckets=max_dirty_buckets,
                                 max_repair_seconds=max_repair_seconds,
                                 bucket_timeout_s=bucket_timeout_s)
    return {"rows": consumed, "buckets": len(buckets_touched), "caught_up": caught_up,
            "cursor": cursor_id, **repairs}
