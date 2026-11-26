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
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
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
            CallRecord.hit, CallRecord.cost_observed_micro, CallRecord.verdict, CallRecord.outcome,
        ).where(CallRecord.endpoint_id == endpoint_id,
                CallRecord.created_at >= start, CallRecord.created_at < start + timedelta(days=1),
                CallRecord.id <= state.cursor_id,
                CallRecord.refused_by.is_(None)).execution_options(yield_per=BATCH_ROWS))
        try:
            # Cross the async result boundary once per bounded batch, then unpack rows locally.
            # The query still reads the complete folded day from one source snapshot.
            async for batch in rows.partitions(BATCH_ROWS):
                for status_code, created_at, duration_ms, hit, cost, verdict, outcome in batch:
                    tally.fold(status_code=status_code, created_at=created_at,
                               duration_ms=duration_ms, hit=hit, cost_observed_micro=cost,
                               refused_by=None, verdict=verdict, empty_fails=empty_fails,
                               outcome=outcome)
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
                       CallRecord.cost_observed_micro, CallRecord.refused_by, CallRecord.verdict,
                       CallRecord.outcome)
                .where(CallRecord.id > state.cursor_id).order_by(CallRecord.id).limit(limit)
            )).all()
            touched: dict[tuple[str, str], stats.Tally] = {}   # this batch only, read under the lock
            reconciled: dict[tuple[str, str], bool] = {}
            batch_touched: set[tuple[str, str]] = set()
            last: tuple[int, datetime] | None = None
            deferred = False
            for (row_id, endpoint_id, status_code, created_at, duration_ms, hit, cost, refused_by, verdict,
                 outcome) in rows:
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
                              verdict=verdict, empty_fails=catalog_store.empty_is_failure(endpoint_id),
                              outcome=outcome):
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
