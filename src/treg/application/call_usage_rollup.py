"""Fold `callrecord` into per-team, per-member, per-kind, per-tool, per-client daily counts (`CallUsageDay`).

Why this exists: the Usage page's counts and the observed-agents roster grouped a team's whole
30-day window of the audit table on every page open. The index finds the window, but every row is
a heap fetch of a wide audit row, so a busy team's report outlasted the report session's statement
timeout. Here the same rows are consumed once, incrementally, by the `treg-worker usage rollup`
cron, and the reports read a few rows per member and day plus the rows after the cursor
(`application/call_usage.py`).

How it walks the table: by primary key from a persisted cursor, exactly as `catalog_stats` does,
and for the same reasons. Rows younger than `LAG` wait for the next run, and everything from the
first young row onward is deferred, so a row still uncommitted when a run passes its id is never
skipped. Audit is lossy by design, so the one remaining race (a row queued for longer than the lag
before its insert) only undercounts by a row it already might have lost; there is no recount. The
first run starts at the first row of `HISTORY_DAYS`, found by bisection on the primary key.

Each batch is its own short transaction: lock the cursor row, read the next rows by id, add them
to their day rows in the database (`calls = calls + ...`), move the cursor, commit. The cursor and
the counts move together, which is what lets a reader add the rows after `cursor_id` without
counting any row twice. The lock is a no-op UPDATE, as in `identity.lock_user`: SQLite ignores
SELECT FOR UPDATE, and two overlapping runs there would fold the same rows twice.

A new cursor starts a new history: the day rows of an earlier one are deleted in the same
transaction. So deleting the cursor (the fallback to the live aggregate) and running the cron
again rebuilds the rollup instead of adding a second copy of it.

This module is the only writer of `callusageday` and `callusagecursor` (team deletion clears a
team's rows with the team).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..infra.db import session_maker
from ..models import CallRecord, CallUsageCursor, CallUsageDay
from ..timeutil import utcnow_naive
from .catalog_stats import _first_id_at

CURSOR_ID = "callrecord"
HISTORY_DAYS = 31          # the first backfill: today and the 30 days before it
LAG = timedelta(seconds=60)  # same allowance `catalog_stats` gives audit commits
BATCH_ROWS = 5_000
UPSERT_ROWS = 500          # day rows per INSERT, well inside both drivers' bind-parameter limits

Bucket = tuple[int, str, str, str, str, str]   # (org_id, day, user_email, kind, tool_name, client)


def _insert(db: AsyncSession):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def _midnight(at: datetime) -> datetime:
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def _day(at: datetime) -> str:
    return at.strftime("%Y-%m-%d")


async def _upsert(db: AsyncSession, buckets: dict[Bucket, list], at: datetime) -> None:
    insert = _insert(db)
    later = func.greatest if db.get_bind().dialect.name == "postgresql" else func.max
    table = CallUsageDay.__table__
    values = [dict(org_id=org_id, day=day, user_email=email, kind=kind, tool_name=tool, client=client,
                   calls=calls, last_at=last_at, updated_at=at)
              for (org_id, day, email, kind, tool, client), (calls, last_at) in sorted(buckets.items())]
    for i in range(0, len(values), UPSERT_ROWS):
        stmt = insert(CallUsageDay).values(values[i:i + UPSERT_ROWS])
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["org_id", "day", "user_email", "kind", "tool_name", "client"],
            set_={"calls": table.c.calls + stmt.excluded.calls,
                  "last_at": later(table.c.last_at, stmt.excluded.last_at),
                  "updated_at": stmt.excluded.updated_at}))


async def _lock_cursor(db: AsyncSession) -> CallUsageCursor | None:
    return (await db.execute(
        update(CallUsageCursor).where(CallUsageCursor.id == CURSOR_ID)
        .values(id=CallUsageCursor.id).returning(CallUsageCursor)
        .execution_options(synchronize_session=False))).scalar_one_or_none()


async def refresh(session_factory=session_maker, *, max_rows: int = 500_000,
                  batch_rows: int = BATCH_ROWS, now: datetime | None = None) -> dict:
    """One scheduled pass: fold up to `max_rows` new audit rows. Returns
    `{rows, buckets, caught_up, cursor}`."""
    at = now or utcnow_naive()
    young = at - LAG
    consumed = 0
    touched: set[Bucket] = set()
    caught_up = False
    cursor_id = 0
    while consumed < max_rows and not caught_up:
        async with session_factory() as db:
            state = await _lock_cursor(db)
            if state is None:
                await db.execute(delete(CallUsageDay))
                start = _midnight(at) - timedelta(days=HISTORY_DAYS - 1)
                state = CallUsageCursor(id=CURSOR_ID, cursor_id=await _first_id_at(db, start) - 1,
                                        history_from=_day(start))
                db.add(state)
            since = datetime.fromisoformat(state.history_from)
            limit = min(batch_rows, max_rows - consumed)
            rows = (await db.execute(
                select(CallRecord.id, CallRecord.org_id, CallRecord.created_at, CallRecord.user_email,
                       CallRecord.kind, CallRecord.tool_name, CallRecord.client)
                .where(CallRecord.id > state.cursor_id).order_by(CallRecord.id).limit(limit))).all()
            buckets: dict[Bucket, list] = {}
            last: tuple[int, datetime] | None = None
            deferred = False
            for row_id, org_id, created_at, email, kind, tool, client in rows:
                if created_at >= young:
                    deferred = True     # still inside the commit lag: this row and everything after it wait
                    break
                last = (int(row_id), created_at)
                consumed += 1
                if org_id is None or created_at < since:
                    continue            # no team to count it for, or before the history (the bisect edge)
                bucket = buckets.setdefault(
                    (int(org_id), _day(created_at), email, kind, tool, client or ""), [0, created_at])
                bucket[0] += 1
                bucket[1] = max(bucket[1], created_at)
            if buckets:
                await _upsert(db, buckets, at)
            touched |= set(buckets)
            if last is not None:
                state.cursor_id, state.watermark = last
            caught_up = deferred or len(rows) < limit
            if caught_up and state.caught_up_at is None:
                state.caught_up_at = at
            state.updated_at = at
            db.add(state)
            cursor_id = state.cursor_id
            await db.commit()
    return {"rows": consumed, "buckets": len(touched), "caught_up": caught_up, "cursor": cursor_id}
