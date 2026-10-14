"""Fold the ledger's `settle` entries into per-team, per-key, per-tool, per-day spend (`ApiKeySpendDay`).

Why this exists: "what did each API key spend" is a sum over every settle entry of the window plus
one audit lookup per charge to find the key. Run on every page open, that is the whole window of a
large team's ledger read into a web process next to the money path. Here the same entries are
consumed once, incrementally, by the `treg-worker spend rollup` cron, and the pages read a few
small rows per key and day.

Attribution (`docs/context/architecture/money.md`, "Spend per API key"): the money is only ever
the ledger's. The audit row supplies the key: `CallRecord.call_ref` is the ledger `call_id` of a
metered call. A routed child (`<ref>:r1`) or an overflow hold (`<ref>:overflow`) settles under a
suffixed id whose own audit row may not exist, so those fall back to the part before the first
colon. A charge with no keyed audit row of the same team (a dashboard or connector call, a hub run,
a call from before keys were recorded, or a row `audit.py` shed) goes to the team's one
"not attributed" line, `api_key_id = 0`. So per-key lines plus that line always equal the ledger.

How it walks the ledger: ledger ids are random, so the position is `(created_at, id)`, read in
that order over the `created_at` index. Entries younger than `LAG` wait for the next run: the
audit row that names the key is written after the settle, through a queue. The first run starts
at the first UTC day of `HISTORY_DAYS`; a capped run (`max_rows`) stops there, and the next run
resumes, so the first backfill catches up over several runs.

The safety net: once a UTC day, after the fold has passed midnight, the last `RECOUNT_DAYS`
complete days are recomputed from the ledger per team and REPLACE the folded rows. It is a lease
on the cursor row, resumable team by team and capped per run (see `recount`). That repairs a
settle that committed after the fold had passed its timestamp, and an audit row that arrived after
the lag. A recount that differs from the fold is logged as a warning.

This module is the only writer of `apikeyspendday` and `apikeyspendcursor` (team deletion clears a
team's rows with the team). It reads `ledgerentry` and `callrecord`, never holds a session longer
than one batch, and commits once per batch.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..infra.db import session_maker
from ..models import ApiKeySpendCursor, ApiKeySpendDay, CallRecord, LedgerEntry
from ..timeutil import utcnow_naive

CURSOR_ID = "ledger"
NOT_ATTRIBUTED = 0
HISTORY_DAYS = 31          # the first backfill: today and the 30 days before it
LAG = timedelta(minutes=5)  # an audit row is queued; give it time to land before reading the key
BATCH_ROWS = 1_000
REF_BATCH = 500            # call refs per `call_ref IN (...)` lookup, over its index
RECOUNT_DAYS = 2
RECOUNT_SECONDS = 60.0                  # one run's time on the recount; the next run resumes
RECOUNT_LEASE = timedelta(minutes=15)   # a run that dies holds the recount no longer than this
log = logging.getLogger("treg.spend")

Bucket = tuple[int, str, int, str]   # (org_id, day, api_key_id, endpoint_id)


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


def _parent(ref: str) -> str:
    return ref.split(":", 1)[0]


async def _keys_of(db: AsyncSession, refs: set[tuple[int, str]]) -> dict[tuple[int, str], int]:
    """`(org_id, call_ref) -> api_key_id` for exactly these refs and their parents, in small
    `call_ref IN (...)` lookups. Several audit rows can share a ref; the first one keys it."""
    wanted = sorted({ref for _, ref in refs if ref} | {_parent(ref) for _, ref in refs if ref})
    found: dict[tuple[int, str], int] = {}
    for i in range(0, len(wanted), REF_BATCH):
        rows = (await db.execute(
            select(CallRecord.org_id, CallRecord.call_ref, CallRecord.api_key_id)
            .where(CallRecord.call_ref.in_(wanted[i:i + REF_BATCH]), CallRecord.api_key_id.is_not(None))
            .order_by(CallRecord.id))).all()
        for org_id, call_ref, key_id in rows:
            found.setdefault((int(org_id), call_ref), int(key_id))
    return found


async def _fold_into(db: AsyncSession, rows, buckets: dict[Bucket, list[int]]) -> None:
    """Add settle rows `(org_id, call_id, endpoint_id, amount_micro, created_at)` to `buckets`."""
    charged = [row for row in rows if row[3]]   # a zero settle moved no money and is no charge
    keys = await _keys_of(db, {(int(org_id), call_id or "") for org_id, call_id, *_ in charged})
    for org_id, call_id, endpoint_id, amount, created_at in charged:
        ref = call_id or ""
        key_id = keys.get((org_id, ref), keys.get((org_id, _parent(ref)), NOT_ATTRIBUTED)) if ref else NOT_ATTRIBUTED
        bucket = buckets.setdefault((int(org_id), _day(created_at), key_id, endpoint_id or ""), [0, 0])
        bucket[0] += -int(amount)
        bucket[1] += 1


def _after(at: datetime, entry_id: str) -> list:
    """Entries after `(at, entry_id)` in `(created_at, id)` order. The bare `created_at >= at` is
    what makes the watermark an index bound: with the whole condition inside one OR, Postgres
    walks the `created_at` index from its first row on every batch. The OR then only orders the
    entries that share the watermark's instant."""
    return [LedgerEntry.created_at >= at,
            or_(LedgerEntry.created_at > at, LedgerEntry.id > entry_id)]


def _values(buckets: dict[Bucket, list[int]], at: datetime) -> list[dict]:
    return [dict(org_id=org_id, day=day, api_key_id=key_id, endpoint_id=endpoint_id,
                 spent_micro=spent, charges=charges, updated_at=at)
            for (org_id, day, key_id, endpoint_id), (spent, charges) in sorted(buckets.items())]


async def _cursor(db: AsyncSession, at: datetime) -> ApiKeySpendCursor:
    """The cursor row, locked for this batch; created at the start of the history on first use."""
    state = (await db.execute(
        select(ApiKeySpendCursor).where(ApiKeySpendCursor.id == CURSOR_ID).with_for_update()
    )).scalar_one_or_none()
    if state is None:
        start = _midnight(at) - timedelta(days=HISTORY_DAYS - 1)
        state = ApiKeySpendCursor(id=CURSOR_ID, watermark_at=start, watermark_id="", history_from=_day(start))
        db.add(state)
    return state


async def refresh(session_factory=session_maker, *, max_rows: int = 50_000,
                  batch_rows: int = BATCH_ROWS, now: datetime | None = None,
                  max_recount_seconds: float = RECOUNT_SECONDS) -> dict:
    """One scheduled pass: fold up to `max_rows` new settle entries, then the daily recount once
    the fold has caught up. Returns `{rows, buckets, caught_up, watermark, recounted}`.

    Each batch is its own short transaction: lock the cursor row, read the next settle entries
    after the watermark, look their keys up, add them to their day rows, move the watermark,
    commit. The lock serializes overlapping runs, and the addition happens in the database
    (`spent_micro = spent_micro + ...`), so nothing read in one batch is written back in another.
    """
    at = now or utcnow_naive()
    young = at - LAG
    consumed = 0
    touched: set[Bucket] = set()
    caught_up = False
    watermark = None
    insert_for = None
    while consumed < max_rows and not caught_up:
        async with session_factory() as db:
            insert_for = insert_for or _insert(db)
            state = await _cursor(db, at)
            limit = min(batch_rows, max_rows - consumed)
            rows = (await db.execute(
                select(LedgerEntry.org_id, LedgerEntry.call_id, LedgerEntry.endpoint_id,
                       LedgerEntry.amount_micro, LedgerEntry.created_at, LedgerEntry.id)
                .where(LedgerEntry.kind == "settle", LedgerEntry.created_at < young,
                       *_after(state.watermark_at, state.watermark_id))
                .order_by(LedgerEntry.created_at, LedgerEntry.id).limit(limit))).all()
            buckets: dict[Bucket, list[int]] = {}
            await _fold_into(db, [row[:5] for row in rows], buckets)
            if buckets:
                stmt = insert_for(ApiKeySpendDay).values(_values(buckets, at))
                table = ApiKeySpendDay.__table__
                await db.execute(stmt.on_conflict_do_update(
                    index_elements=["org_id", "day", "api_key_id", "endpoint_id"],
                    set_={"spent_micro": table.c.spent_micro + stmt.excluded.spent_micro,
                          "charges": table.c.charges + stmt.excluded.charges,
                          "updated_at": stmt.excluded.updated_at}))
            touched |= set(buckets)
            consumed += len(rows)
            if rows:
                state.watermark_at, state.watermark_id = rows[-1][4], rows[-1][5]
            caught_up = len(rows) < limit
            if caught_up and state.caught_up_at is None:
                state.caught_up_at = at
            state.updated_at = at
            db.add(state)
            watermark = state.watermark_at
            await db.commit()
    recounted = await recount(session_factory, now=at, max_seconds=max_recount_seconds) if caught_up else []
    return {"rows": consumed, "buckets": len(touched), "caught_up": caught_up,
            "watermark": watermark.isoformat() if watermark else None, "recounted": len(recounted)}


async def recount(session_factory=session_maker, *, now: datetime | None = None,
                  batch_rows: int = BATCH_ROWS, max_seconds: float = RECOUNT_SECONDS) -> list[dict]:
    """The daily safety net: recompute the last `RECOUNT_DAYS` complete UTC days of every team
    that spent in them, from the ledger, and replace the folded rows. Runs once per UTC day, after
    a fold that caught up past midnight. Returns the teams and days whose recount differed from
    the fold (each also logged).

    A recount can outlast one cron interval, so it is a lease on the cursor row, taken before any
    team is read: `recount_day` names the day being recounted, `recount_owner` the run holding it
    until `recount_lease_until`. A run that finds a live lease held by another run skips the
    recount. Teams go in id order, and each team's rows are replaced in one transaction that
    re-checks the lease under the row lock and records the team as done (`recount_after_org`), so
    a later run resumes after it and two runs never replace the same team's days. One run spends
    at most `max_seconds` on it (always at least one team, so a recount always moves), then hands
    the lease back; a run that dies keeps it only until it expires.
    """
    at = now or utcnow_naive()
    today = _midnight(at)
    started = time.monotonic()

    def clock() -> datetime:   # `at`, moved on by the time this run has spent
        return at + timedelta(seconds=time.monotonic() - started)

    owner = uuid.uuid4().hex
    async with session_factory() as db:
        state = await _cursor(db, at)
        # Called after a fold that caught up, so every entry older than the lag is folded; once
        # that is past midnight, the fold never touches the days before it again.
        if state.caught_up_at is None or state.recounted_on == _day(today) or at - LAG < today:
            await db.rollback()
            return []
        if state.recount_day == _day(today) and state.recount_owner \
                and state.recount_lease_until and state.recount_lease_until > at:
            await db.rollback()
            return []            # another run is recounting today
        if state.recount_day != _day(today):
            state.recount_day, state.recount_after_org = _day(today), 0
        state.recount_owner, state.recount_lease_until = owner, clock() + RECOUNT_LEASE
        state.updated_at = at
        after_org = state.recount_after_org
        first = max(today - timedelta(days=RECOUNT_DAYS), datetime.fromisoformat(state.history_from))
        db.add(state)
        await db.commit()
    days = [_day(first + timedelta(days=i)) for i in range((today - first).days)]
    async with session_factory() as db:
        # The teams that spent on those days, as folded. A team every one of whose charges the fold
        # missed is not found here; that needs each of them to commit later than the lag.
        orgs = (await db.execute(select(ApiKeySpendDay.org_id).where(
            ApiKeySpendDay.day.in_(days), ApiKeySpendDay.org_id > after_org)
            .distinct().order_by(ApiKeySpendDay.org_id))).scalars().all()
    differed: list[dict] = []
    done = 0
    for org_id in orgs:
        if done and time.monotonic() - started >= max_seconds:
            break
        result = await _recount_team(session_factory, int(org_id), first, today, days, at, batch_rows,
                                     owner=owner, clock=clock)
        if result is None:
            return differed       # the lease was lost (expired and taken): that run carries on
        differed += result
        done += 1
    finished = done == len(orgs)
    async with session_factory() as db:
        state = await _cursor(db, at)
        if state.recount_owner == owner:
            if finished:
                state.recounted_on, state.recount_day = _day(today), None
            state.recount_owner, state.recount_lease_until = None, None
            state.updated_at = at
            db.add(state)
        await db.commit()
    return differed


async def _recount_team(session_factory, org_id: int, first: datetime, until: datetime,
                        days: list[str], at: datetime, batch_rows: int, *, owner: str,
                        clock) -> list[dict] | None:
    """One team's recount: read its settle entries over `(org_id, created_at)` in batches, each in
    its own session, then replace its rows for `days` in one short transaction that holds the
    cursor row, checks this run still owns the lease, and marks the team done. None if the lease
    was lost: nothing is written."""
    buckets: dict[Bucket, list[int]] = {}
    after: tuple[datetime, str] = (first, "")
    while True:
        async with session_factory() as db:
            rows = (await db.execute(
                select(LedgerEntry.org_id, LedgerEntry.call_id, LedgerEntry.endpoint_id,
                       LedgerEntry.amount_micro, LedgerEntry.created_at, LedgerEntry.id)
                .where(LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle",
                       LedgerEntry.created_at < until, *_after(*after))
                .order_by(LedgerEntry.created_at, LedgerEntry.id).limit(batch_rows))).all()
            await _fold_into(db, [row[:5] for row in rows], buckets)
        if len(rows) < batch_rows:
            break
        after = (rows[-1][4], rows[-1][5])
    differed = []
    async with session_factory() as db:
        state = await _cursor(db, at)
        if state.recount_owner != owner:
            await db.rollback()
            return None
        folded = (await db.execute(
            select(ApiKeySpendDay.day, func.sum(ApiKeySpendDay.spent_micro), func.sum(ApiKeySpendDay.charges))
            .where(ApiKeySpendDay.org_id == org_id, ApiKeySpendDay.day.in_(days))
            .group_by(ApiKeySpendDay.day))).all()
        before = {day: (int(spent or 0), int(charges or 0)) for day, spent, charges in folded}
        for day in days:
            now_spent = sum(v[0] for k, v in buckets.items() if k[1] == day)
            now_charges = sum(v[1] for k, v in buckets.items() if k[1] == day)
            if before.get(day, (0, 0)) != (now_spent, now_charges):
                was = before.get(day, (0, 0))
                differed.append({"org_id": org_id, "day": day, "folded_micro": was[0], "recounted_micro": now_spent,
                                 "folded_charges": was[1], "recounted_charges": now_charges})
                log.warning("spend rollup recount differs: org=%s day=%s folded=%s/%s recounted=%s/%s",
                            org_id, day, was[0], was[1], now_spent, now_charges)
        # Key attribution can move without the day's total moving, so the rows are always replaced.
        await db.execute(delete(ApiKeySpendDay).where(
            ApiKeySpendDay.org_id == org_id, ApiKeySpendDay.day.in_(days)))
        if buckets:
            await db.execute(_insert(db)(ApiKeySpendDay).values(_values(buckets, at)))
        state.recount_after_org, state.recount_lease_until = org_id, clock() + RECOUNT_LEASE
        db.add(state)
        await db.commit()
    return differed
