"""The month partitions of a partitioned `callrecord`: create the coming ones, drop the expired
ones; and copy history from the plain `callrecord` into `callrecord_partitioned`, the partitioned
table alembic 0116 builds beside it (docs/context/architecture/data-model.md).

A row whose month has no partition cannot be inserted, so once `callrecord` is partitioned the
coming months must exist before they start: `maintain()` runs at every release
(`maintenance.RELEASE_TASKS`) and on the `treg-worker usage rollup` cron.

A partition is created as a plain table and attached, never with `CREATE TABLE ... PARTITION OF`:
attaching takes SHARE UPDATE EXCLUSIVE on `callrecord`, which audit inserts and readers do not
wait for, where `PARTITION OF` takes ACCESS EXCLUSIVE. For the same reason there is no default
partition: with one, every attach locks and scans it, and `DETACH ... CONCURRENTLY` is refused.

Retention is `callrecord_retention_months` (0, the default, keeps everything): a month older than
that is detached with `DETACH PARTITION ... CONCURRENTLY` and dropped, which returns its space to
the operating system at once and writes almost no WAL. A `DELETE` of the same rows would do neither.

Every function here is a no-op on a database where `callrecord` is not partitioned (SQLite, or a
PostgreSQL database built from the models). This module is the only writer of these partitions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, timedelta

from sqlalchemy import text

from ..config import get_settings
from ..infra.db import session_maker
from ..timeutil import utcnow_naive

TABLE = "callrecord"
STAGED = "callrecord_partitioned"   # the partitioned table alembic 0116 builds, filled by `backfill`
MONTHS_AHEAD = 3
LOCK_TIMEOUT = "3s"
DETACH_TIMEOUT = "60s"
# Ids are handed out before their rows commit, so a copy that continues above the last copied id
# starts this many ids below it.
COMMIT_MARGIN = 20_000
# The staged table's comment (JSON) carries when the first backfill run started: rows copied then
# can still receive a late `hit`/`verdict`, so the swap resyncs from that moment, not from "now".
NOTE_BACKFILL_STARTED = "backfill_started_at"
# The copy runs on the primary beside live traffic and its WAL is replayed by every replica, so by
# default it leaves the database idle about as long as each batch keeps it busy.
BACKFILL_BATCH_ROWS = 5_000
BACKFILL_PAUSE_SECONDS = 0.5
_NAME = re.compile(rf"^{TABLE}_y(\d{{4}})m(\d{{2}})$")
_ADVISORY_KEY = 0x63726563  # one creator at a time; any constant no other advisory lock uses
_NO_BOUND = 2**62

log = logging.getLogger("treg.call_record_partitions")


def _month(day: date, offset: int = 0) -> date:
    index = day.year * 12 + day.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _name(month: date) -> str:
    return f"{TABLE}_y{month:%Y}m{month:%m}"


async def _relkind(db, table: str) -> str | None:
    if db.get_bind().dialect.name != "postgresql":
        return None
    return (await db.execute(text(
        "SELECT relkind::text FROM pg_class WHERE oid = to_regclass(:name)"), {"name": table})).scalar()


async def _month_tables(db, parent: str = TABLE) -> dict[date, tuple[str, bool, bool]]:
    """month -> (table name, attached, detach left unfinished), for the tables this module named.
    A table that is not attached was detached by a run that stopped before dropping it."""
    rows = (await db.execute(text(
        "SELECT c.relname, i.inhrelid IS NOT NULL, coalesce(i.inhdetachpending, false) "
        "FROM pg_class c LEFT JOIN pg_inherits i "
        "ON i.inhrelid = c.oid AND i.inhparent = to_regclass(:name) "
        "WHERE c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass(:name)) "
        "AND c.relkind = 'r' AND c.relname LIKE :pattern"),
        {"name": parent, "pattern": f"{TABLE}\\_y%"})).all()
    found = {}
    for name, attached, pending in rows:
        if match := _NAME.match(name):
            found[date(int(match[1]), int(match[2]), 1)] = (name, bool(attached), bool(pending))
    return found


async def _attached(db, parent: str) -> set[date]:
    return {month for month, (_, attached, _) in (await _month_tables(db, parent)).items() if attached}


async def _attach(db, month: date, parent: str) -> None:
    name, end = _name(month), _month(month, 1)
    await db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_KEY})
    if month in await _attached(db, parent):
        return
    await db.execute(text(f"CREATE TABLE IF NOT EXISTS {name} (LIKE {parent} INCLUDING DEFAULTS)"))
    await db.execute(text(
        f"ALTER TABLE {parent} ATTACH PARTITION {name} FOR VALUES FROM ('{month}') TO ('{end}')"))


async def ensure(months: list[date] | None = None, *, parent: str = TABLE,
                 session_factory=session_maker) -> list[str]:
    """Create the missing partitions among `months` (default: this month and the next
    `MONTHS_AHEAD`), each in its own transaction, and return their names."""
    created = []
    async with session_factory() as db:
        if await _relkind(db, parent) != "p":
            return created
        if months is None:
            today = utcnow_naive().date()
            months = [_month(today, offset) for offset in range(MONTHS_AHEAD + 1)]
        missing = sorted(set(months) - await _attached(db, parent))
        await db.commit()
        for month in missing:
            await _attach(db, month, parent)
            await db.commit()
            created.append(_name(month))
    return created


async def drop_expired(retention_months: int, *, session_factory=session_maker) -> list[str]:
    """Detach and drop every partition older than `retention_months` full months before the
    current one. A detach that was interrupted is finished first."""
    dropped = []
    if retention_months < 1:
        return dropped
    async with session_factory() as db:
        if await _relkind(db, TABLE) != "p":
            return dropped
        cutoff = _month(utcnow_naive().date(), -retention_months)
        expired = sorted((month, *state) for month, state in (await _month_tables(db)).items()
                         if month < cutoff)
        await db.commit()
        if not expired:
            return dropped
        # DETACH ... CONCURRENTLY runs as two transactions of its own, so it cannot run in one.
        connection = await db.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
        try:
            await connection.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
            await connection.execute(text(f"SET statement_timeout = '{DETACH_TIMEOUT}'"))
            for _, name, attached, pending in expired:
                if attached:
                    how = "FINALIZE" if pending else "CONCURRENTLY"
                    await connection.execute(text(f"ALTER TABLE {TABLE} DETACH PARTITION {name} {how}"))
                await connection.execute(text(f"DROP TABLE {name}"))
                dropped.append(name)
        finally:
            await connection.execute(text("RESET lock_timeout"))
            await connection.execute(text("RESET statement_timeout"))
    return dropped


async def maintain(*, session_factory=session_maker) -> dict:
    """One pass: the coming partitions exist, and with retention on, the expired ones are gone."""
    created = await ensure(session_factory=session_factory)
    dropped = await drop_expired(get_settings().callrecord_retention_months,
                                 session_factory=session_factory)
    if created or dropped:
        log.info("callrecord partitions: created %s, dropped %s", created, dropped)
    return {"created": created, "dropped": dropped}


async def ensure_for_release() -> int:
    return len(await ensure())


async def _copy_ids(db, source: str, target: str, columns: str, first: int, below: int,
                    session_factory) -> int:
    """Copy the rows with `first <= id < below`, after creating the partitions their months need."""
    oldest, newest = (await db.execute(text(
        f"SELECT min(created_at), max(created_at) FROM {source} "
        "WHERE id >= :first AND id < CAST(:below AS bigint)"), {"first": first, "below": below})).one()
    await db.commit()
    if oldest is None:
        return 0
    span = (newest.year - oldest.year) * 12 + newest.month - oldest.month + 1
    await ensure([_month(oldest.date(), offset) for offset in range(span)], parent=target,
                 session_factory=session_factory)
    result = await db.execute(text(
        f"INSERT INTO {target} ({columns}) SELECT {columns} FROM {source} "
        "WHERE id >= :first AND id < CAST(:below AS bigint) ON CONFLICT DO NOTHING"),
        {"first": first, "below": below})
    await db.commit()
    return int(result.rowcount or 0)


async def staged_note(db) -> dict:
    """The JSON note on the staged table (empty when there is none or it is not JSON)."""
    raw = (await db.execute(text("SELECT obj_description(to_regclass(:name), 'pg_class')"),
                            {"name": STAGED})).scalar()
    try:
        note = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


async def _note_first_run(db) -> None:
    """Record when the first backfill run started, once; later runs keep the first moment."""
    note = await staged_note(db)
    if NOTE_BACKFILL_STARTED in note:
        await db.commit()
        return
    note[NOTE_BACKFILL_STARTED] = utcnow_naive().isoformat()
    # COMMENT takes no bind parameters; the note is JSON this code wrote, quotes doubled.
    literal = json.dumps(note, sort_keys=True).replace("'", "''")
    await db.execute(text(f"COMMENT ON TABLE {STAGED} IS '{literal}'"))
    await db.commit()


async def backfill(days: int, *, batch_rows: int = BACKFILL_BATCH_ROWS, max_seconds: float | None = None,
                   pause: float = BACKFILL_PAUSE_SECONDS, session_factory=session_maker) -> dict:
    """Copy the last `days` days of call records from the plain `callrecord` into
    `callrecord_partitioned`, which nothing reads yet -> {phase, copied, batches, reached, done}
    (`phase: "before_swap"`, or None when there is nothing to copy into).

    Rows keep their ids. Each batch is one transaction over a bounded id range, and no batch
    takes a lock a reader or a writer waits for. A run first copies what was written since the
    last one, then continues below the lowest id the target holds, newest first, until the rows
    are older than `days`. A run that stops (`max_seconds`, a failure) is
    resumed by running it again; `done` is true when the cutoff or the first row was reached.
    """
    if days < 1 or batch_rows < 1:
        raise ValueError("days and batch_rows must be >= 1")
    started = time.monotonic()
    cutoff = utcnow_naive() - timedelta(days=days)
    out = {"phase": None, "copied": 0, "batches": 0, "reached": None, "done": True}

    def spent() -> bool:
        return max_seconds is not None and time.monotonic() - started >= max_seconds

    async with session_factory() as db:
        if await _relkind(db, TABLE) == "r" and await _relkind(db, STAGED) == "p":
            source, target, out["phase"] = TABLE, STAGED, "before_swap"
        else:
            return out
        names = (await db.execute(text(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(:name) "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"), {"name": source})).scalars().all()
        columns = ", ".join(f'"{name}"' for name in names)
        oldest_copied, newest_copied = (await db.execute(text(
            f"SELECT min(id), max(id) FROM {target}"))).one()
        await db.commit()
        if out["phase"] == "before_swap":
            await _note_first_run(db)

        if out["phase"] == "before_swap" and newest_copied is not None:
            top = (await db.execute(text(f"SELECT max(id) FROM {source}"))).scalar() or 0
            for first in range(max(newest_copied - COMMIT_MARGIN, oldest_copied), top + 1, batch_rows):
                out["copied"] += await _copy_ids(db, source, target, columns, first,
                                                 min(first + batch_rows, top + 1), session_factory)
                out["batches"] += 1
                if spent():
                    return {**out, "done": False}
                await asyncio.sleep(pause)

        while True:
            below = (await db.execute(text(f"SELECT min(id) FROM {target}"))).scalar() or _NO_BOUND
            first, oldest, newest = (await db.execute(text(
                f"SELECT min(id), min(created_at), max(created_at) FROM ("
                f"SELECT id, created_at FROM {source} WHERE id < CAST(:below AS bigint) "
                f"ORDER BY id DESC LIMIT :rows) batch"), {"below": below, "rows": batch_rows})).one()
            await db.commit()
            if first is None or newest < cutoff:
                return out
            out["copied"] += await _copy_ids(db, source, target, columns, first, below, session_factory)
            out["batches"] += 1
            out["reached"] = oldest.isoformat()
            if spent():
                return {**out, "done": False}
            await asyncio.sleep(pause)
