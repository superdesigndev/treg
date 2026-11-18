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
from contextlib import asynccontextmanager
import logging
import re
import time
from datetime import date, datetime, timedelta

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
# Late `hit`/`verdict` values: how far the backfill's resync got, when that pass began, and the
# cursor of a pass that has not finished. The swap reads them.
NOTE_RESYNCED_UPTO = "resynced_upto"
NOTE_RESYNCED_AT = "resynced_at"
NOTE_RESYNC_PASS = "resync_pass"
# A verdict lands at most a day after its row (an async task's answer is kept 24 h); the resync
# looks back a little further.
RESYNC_LOOK_BACK = timedelta(hours=25)
# The copy runs on the primary beside live traffic and its WAL is replayed by every replica, so by
# default it leaves the database idle about as long as each batch keeps it busy.
BACKFILL_BATCH_ROWS = 5_000
BACKFILL_PAUSE_SECONDS = 0.5
REPLICA_LAG_POLL_SECONDS = 5.0
# One backfill at a time (pg_try_advisory_lock); a constant no other advisory lock uses.
BACKFILL_LOCK_KEY = 0x63726266
DELETED_TEAM_BATCH = 10_000
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


async def _columns(db, table: str) -> str:
    names = (await db.execute(text(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(:name) "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"), {"name": table})).scalars().all()
    return ", ".join(f'"{name}"' for name in names)


async def _copy_ids(db, source: str, target: str, first: int, below: int, session_factory) -> int:
    """Copy the rows with `first <= id < below`, after creating the partitions their months need.
    The column list is read again for every batch, so a column added meanwhile is copied too."""
    columns = await _columns(db, source)
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
    await _write_note(db, note)


async def replica_lag_seconds() -> float:
    """How far the read replica's replay is behind, in seconds; 0 without a replica (the read
    pool is then the primary, where `pg_last_xact_replay_timestamp()` is NULL)."""
    from ..infra.db import read_session_maker

    async with read_session_maker() as db:
        lag = (await db.execute(text(
            "SELECT EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())"))).scalar()
    return max(float(lag or 0), 0.0)


async def delete_rows_of_deleted_teams(*, table: str = STAGED, batch: int = DELETED_TEAM_BATCH,
                                       between_batches=None, session_factory=session_maker) -> int:
    """Delete from a partitioned `table` the rows of teams that no longer exist, `batch` rows per
    committed transaction -> rows deleted. A team deleted while a backfill batch was copying its
    rows can leave copies behind. Postgres cannot skip through an index, so the distinct teams
    are walked one `(org_id, id)` probe each, and each kept only `NOT EXISTS` in `org`; an
    anti-join against `org` would read every row."""
    deleted = 0
    async with session_factory() as db:
        if await _relkind(db, table) != "p":
            return 0
        gone = (await db.execute(text(f"""
            WITH RECURSIVE team AS (
                SELECT min(org_id) AS org_id FROM {table}
                UNION ALL
                SELECT (SELECT min(org_id) FROM {table} WHERE org_id > team.org_id)
                FROM team WHERE team.org_id IS NOT NULL
            )
            SELECT team.org_id FROM team
            WHERE team.org_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM org WHERE org.id = team.org_id)
        """))).scalars().all()
        await db.commit()
        for org_id in gone:
            while True:
                result = await db.execute(text(
                    f"DELETE FROM {table} WHERE (id, created_at) IN (SELECT id, created_at FROM {table} "
                    "WHERE org_id = :org_id ORDER BY id LIMIT :batch)"), {"org_id": org_id, "batch": batch})
                await db.commit()
                deleted += int(result.rowcount or 0)
                if (result.rowcount or 0) < batch:
                    break
                if between_batches is not None:
                    await between_batches()
    return deleted


async def backfill(days: int, *, batch_rows: int = BACKFILL_BATCH_ROWS, max_seconds: float | None = None,
                   pause: float = BACKFILL_PAUSE_SECONDS, max_replica_lag: float | None = None,
                   session_factory=session_maker) -> dict:
    """Copy the last `days` days of call records from the plain `callrecord` into
    `callrecord_partitioned`, which nothing reads yet
    -> {phase, copied, batches, reached, done, deleted_orphans, replica_waits, resynced}, with
    `busy: True` and nothing done when another backfill holds the lock
    (`phase: "before_swap"`, or None when there is nothing to copy into).

    Rows keep their ids. Each batch is one transaction over a bounded id range, and no batch
    takes a lock a reader or a writer waits for. A run first copies what was written since the
    last one, then continues below the lowest id the target holds, newest first, until the rows
    are older than `days`. A run that stops (`max_seconds`, a failure) is resumed by running it
    again; `done` is true when the cutoff or the first row was reached.

    Each run first deletes the copied rows of teams deleted since (`delete_rows_of_deleted_teams`),
    and last resyncs late `hit`/`verdict` values (`_resync_late_verdicts`). With
    `max_replica_lag` (seconds), it waits between batches while the read replica is further behind
    than that.
    """
    if days < 1 or batch_rows < 1:
        raise ValueError("days and batch_rows must be >= 1")
    started = time.monotonic()
    cutoff = utcnow_naive() - timedelta(days=days)
    run_started = utcnow_naive()
    out = {"phase": None, "copied": 0, "batches": 0, "reached": None, "done": True,
           "deleted_orphans": 0, "replica_waits": 0, "resynced": 0}

    def spent() -> bool:
        return max_seconds is not None and time.monotonic() - started >= max_seconds

    async with _only_backfill(session_factory) as alone:
        if not alone:
            return {**out, "done": False, "busy": True}
        return await _backfill(out, cutoff=cutoff, run_started=run_started, batch_rows=batch_rows,
                               pause=pause, max_replica_lag=max_replica_lag, spent=spent,
                               session_factory=session_factory)


@asynccontextmanager
async def _only_backfill(session_factory):
    """One backfill at a time: a session-level advisory lock on a connection of its own (idle
    between its statements, so it holds no snapshot) -> whether this run got it."""
    async with session_factory.kw["bind"].connect() as connection:
        if connection.dialect.name != "postgresql":
            yield True
            return
        got = bool((await connection.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                             {"key": BACKFILL_LOCK_KEY})).scalar_one())
        await connection.commit()
        try:
            yield got
        finally:
            if got:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"),
                                         {"key": BACKFILL_LOCK_KEY})
                await connection.commit()


async def _backfill(out: dict, *, cutoff, run_started, batch_rows: int, pause: float,
                    max_replica_lag: float | None, spent, session_factory) -> dict:
    async def between_batches() -> None:
        await asyncio.sleep(pause)
        while (max_replica_lag is not None and not spent()
               and await replica_lag_seconds() > max_replica_lag):
            out["replica_waits"] += 1
            await asyncio.sleep(REPLICA_LAG_POLL_SECONDS)

    async with session_factory() as db:
        if await _relkind(db, TABLE) == "r" and await _relkind(db, STAGED) == "p":
            source, target, out["phase"] = TABLE, STAGED, "before_swap"
        else:
            return out
        oldest_copied, newest_copied = (await db.execute(text(
            f"SELECT min(id), max(id) FROM {target}"))).one()
        await db.commit()
        await _note_first_run(db)
        out["deleted_orphans"] = await delete_rows_of_deleted_teams(
            table=target, between_batches=between_batches, session_factory=session_factory)

        async def copy() -> bool:
            if newest_copied is not None:
                top = (await db.execute(text(f"SELECT max(id) FROM {source}"))).scalar() or 0
                for first in range(max(newest_copied - COMMIT_MARGIN, oldest_copied), top + 1,
                                   batch_rows):
                    out["copied"] += await _copy_ids(db, source, target, first,
                                                     min(first + batch_rows, top + 1), session_factory)
                    out["batches"] += 1
                    if spent():
                        return False
                    await between_batches()
            while True:
                below = (await db.execute(text(f"SELECT min(id) FROM {target}"))).scalar() or _NO_BOUND
                first, oldest, newest = (await db.execute(text(
                    f"SELECT min(id), min(created_at), max(created_at) FROM ("
                    f"SELECT id, created_at FROM {source} WHERE id < CAST(:below AS bigint) "
                    f"ORDER BY id DESC LIMIT :rows) batch"), {"below": below, "rows": batch_rows})).one()
                await db.commit()
                if first is None or newest < cutoff:
                    return True
                out["copied"] += await _copy_ids(db, source, target, first, below, session_factory)
                out["batches"] += 1
                out["reached"] = oldest.isoformat()
                if spent():
                    return False
                await between_batches()

        if not await copy():
            return {**out, "done": False}
        if not await _resync_late_verdicts(db, out, run_started=run_started, batch_rows=batch_rows,
                                           spent=spent, between_batches=between_batches):
            return {**out, "done": False}
        return out


async def _write_note(db, note: dict) -> None:
    # COMMENT takes no bind parameters; the note is JSON this code wrote, quotes doubled.
    literal = json.dumps(note, sort_keys=True).replace("'", "''")
    await db.execute(text(f"COMMENT ON TABLE {STAGED} IS '{literal}'"))
    await db.commit()


async def _first_id_at(db, since: datetime, top: int) -> int:
    """The smallest id of `callrecord` whose row is at or after `since`, by bisection on the
    primary key: ids and `created_at` rise together, and there is no index on `created_at` alone."""
    low, high = 0, top + 1
    while low < high:
        mid = (low + high) // 2
        row = (await db.execute(text(
            f"SELECT id, created_at FROM {TABLE} WHERE id >= :mid ORDER BY id LIMIT 1"),
            {"mid": mid})).first()
        if row is None or row.created_at >= since:
            high = mid
        else:
            low = row.id + 1
    await db.commit()
    return low


_RESYNC = text(
    f"UPDATE {STAGED} n SET hit = o.hit, verdict = o.verdict FROM {TABLE} o "
    # Bounded on both sides, so a batch reads its id range of the staged table, not all of it.
    "WHERE o.id > :after AND o.id <= :upto AND n.id > :after AND n.id <= :upto "
    "AND n.id = o.id AND n.created_at = o.created_at "
    "AND (n.hit IS DISTINCT FROM o.hit OR n.verdict IS DISTINCT FROM o.verdict)")


async def _resync_late_verdicts(db, out: dict, *, run_started: datetime, batch_rows: int, spent,
                                between_batches) -> bool:
    """Give the copied rows the late `hit`/`verdict` values the live table got since they were
    copied -> False when the time budget stopped it (it resumes on the next run).

    A verdict lands at most `RESYNC_LOOK_BACK` after its row (an async task's answer is kept 24 h).
    A pass covers the rows created from `RESYNC_LOOK_BACK` before the previous pass began (before
    this run, for the first) up to the top copied id, then one short pass up to the top id copied
    meanwhile. It notes its cursor after each batch (`resync_pass`), and on completion
    `resynced_upto` and `resynced_at` (when it began), which the swap reads."""
    note = await staged_note(db)
    top = int((await db.execute(text(f"SELECT max(id) FROM {STAGED}"))).scalar() or 0)
    await db.commit()
    unfinished = note.get(NOTE_RESYNC_PASS)
    if isinstance(unfinished, dict):
        began, after = unfinished["began"], int(unfinished["cursor"])
    else:
        began = run_started.isoformat()
        # No completed pass yet: the first copy began with the first run, which may be a
        # stopped one days ago, not this one.
        since = datetime.fromisoformat(
            note.get(NOTE_RESYNCED_AT) or note.get(NOTE_BACKFILL_STARTED) or began)
        after = max(await _first_id_at(db, since - RESYNC_LOOK_BACK, top) - 1, 0)
    while after < top:
        upto = min(after + batch_rows, top)
        result = await db.execute(_RESYNC, {"after": after, "upto": upto})
        out["resynced"] += int(result.rowcount or 0)
        await db.commit()
        after = upto
        note[NOTE_RESYNC_PASS] = {"began": began, "cursor": after}
        await _write_note(db, note)
        if spent() and after < top:
            return False
        await between_batches()
    # One short pass up to what is copied now: rows another run copied while this pass ran.
    latest = int((await db.execute(text(f"SELECT max(id) FROM {STAGED}"))).scalar() or 0)
    if latest > after:
        result = await db.execute(_RESYNC, {"after": after, "upto": latest})
        out["resynced"] += int(result.rowcount or 0)
        await db.commit()
        after = latest
    note.pop(NOTE_RESYNC_PASS, None)
    note[NOTE_RESYNCED_UPTO], note[NOTE_RESYNCED_AT] = after, began
    await _write_note(db, note)
    return True
