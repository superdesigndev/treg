"""Fold `callrecord` into per-endpoint, per-day reliability buckets (`EndpointDayStat`).

Why this exists: the catalog's "does it still work, how fast" numbers were computed on demand by
`domain.catalog.stats.observed`, a thirty-day aggregate over the audit table run by every web
process every time its five-minute cache expired (and again from cold after each deploy). On a
large audit table that query runs for tens of seconds, and its pages evict the ones the money path
needs. Here the same evidence is consumed once, incrementally, by the `treg-worker catalog stats`
cron, and the request path reads thirty small rows per endpoint.

How it walks the table: by primary key from a persisted cursor, never by `created_at` (the audit
table has no bare `created_at` index, and does not need one for this). Rows younger than `LAG`
are left for the next run: an audit insert commits a few seconds after its `created_at`, and the
ids are handed out at insert, so a row that is still uncommitted when a run passes its id is
always younger than the lag and everything from the first young row onward is deferred. Audit is
lossy by design (`audit.py` sheds rows under load), so the one remaining race, a row queued for
longer than the lag before its insert, only ever undercounts a day by a row it already might have
lost. The first run starts at the id of the first row inside the window, found by bisection on
the primary key so the backfill never scans a row older than the window; until a run drains the
backlog the reader keeps computing observations live, so nothing changes for a deployment that
has not scheduled the worker.

This module is the only writer of `endpointdaystat` and `endpointstatcursor`. It reads
`callrecord` and nothing else, commits once per batch, and holds no session across anything but
those two tables.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.catalog import stats
from ..domain.catalog import store as catalog_store
from ..infra.db import session_maker
from ..models import CallRecord, EndpointDayStat, EndpointStatCursor
from ..timeutil import utcnow_naive

CURSOR_ID = "callrecord"
LAG = timedelta(seconds=60)      # same allowance the Arena collector gives audit commits
BATCH_ROWS = 5_000
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
                  batch_rows: int = BATCH_ROWS, now: datetime | None = None) -> dict:
    """One scheduled pass. Returns `{rows, buckets, caught_up, cursor}`.

    Each batch is its own short transaction: lock the cursor row, read the next `batch_rows` audit
    rows by id, read the current value of every day bucket they touch, fold, upsert those buckets,
    advance the cursor, commit. Nothing about a bucket is carried from one batch to the next: the
    row lock serializes overlapping runs (a slow backfill still running when the next schedule
    fires), and a bucket value remembered from an earlier batch would be stale by the time the lock
    is held again, so the later upsert would erase what the other run folded in between, with the
    cursor already past those rows and nothing to recover them from.
    """
    at = now or utcnow_naive()
    young = at - LAG
    since = at - timedelta(days=stats.WINDOW_DAYS)
    consumed = 0
    buckets_touched: set[tuple[str, str]] = set()
    caught_up = False
    cursor_id = 0
    insert_for = None
    while consumed < max_rows and not caught_up:
        async with session_factory() as db:
            insert_for = insert_for or _insert(db)
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
                if tally.fold(status_code=status_code, created_at=created_at, duration_ms=duration_ms,
                              hit=hit, cost_observed_micro=cost, refused_by=refused_by,
                              verdict=verdict, empty_fails=catalog_store.empty_is_failure(endpoint_id)):
                    batch_touched.add(key)
            buckets_touched |= batch_touched
            for endpoint_id, day in sorted(batch_touched):
                t = touched[(endpoint_id, day)]
                values = dict(endpoint_id=endpoint_id, day=day, n=t.n, ok=t.ok, bad=t.bad,
                              last_ok_at=t.last_ok, hits=t.hits, hit_decided=t.hit_decided,
                              paid_hits=t.paid_hits, free_misses=t.free_misses,
                              latency_seen=t.latency_seen, latency_sample=t.latencies,
                              verdicts=t.verdicts, updated_at=at)
                stmt = insert_for(EndpointDayStat).values(**values)
                await db.execute(stmt.on_conflict_do_update(
                    index_elements=["endpoint_id", "day"],
                    set_={k: getattr(stmt.excluded, k) for k in values if k not in ("endpoint_id", "day")}))
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
    return {"rows": consumed, "buckets": len(buckets_touched), "caught_up": caught_up, "cursor": cursor_id}
