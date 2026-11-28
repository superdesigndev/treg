"""the partitioned call record table takes the name `callrecord`

Revision ID: 0142
Revises: 0141
Create Date: 2026-11-18

After this revision `callrecord` is the monthly-partitioned, 64-bit table that 0141 built, and the
table it replaces is `callrecord_legacy`, which nothing writes any more. No reader changes: the
name, the columns and the index names are the same, and the new table already holds the history
the readers use.

**The history is copied before the swap, not after it.** Several readers count "today" or rebuild
a recent window from call records (the trial allowance, the spend recount, catalog statistics, the
usage rollup); a table that started with only its newest rows would give them wrong answers.

- A small table (at most `_INLINE_IDS` ids from first to last) is copied here, whole.
- A larger one must be copied first by `treg-worker admin callrecord-backfill --days N`, which
  runs for as long as it needs beside live traffic. This revision refuses, changing nothing,
  until the partitioned table reaches back `_MIN_HISTORY_DAYS` days or to the first row, and
  while more than `_MAX_CATCH_UP_IDS` ids were written since the last backfill run: run the
  backfill again just before the deploy, so the catch-up below is short.

Late `hit`/`verdict`/`cost_charged_micro` values (written up to a day after the row) are resynced by the backfill,
each run from where the run before began; this revision resyncs only the rows above the
`resynced_upto` the last run noted on the new table. It refuses, changing nothing, when that range
is over `_MAX_RESYNC_IDS` ids or the backfill's last resync did not finish: run the backfill again.
A late verdict on a row the last run already resynced, landing between that run and the swap,
stays in the legacy table only.

The steps, of which only the last blocks anyone:

1. **Catch up** (no lock; batches by id range): the rows written since the last copy, repeated
   until a few hundred ids are left. Month partitions are created as needed.
2. **Late verdicts** (no lock): the rows above `resynced_upto`, which is moved after each batch,
   so an upgrade stopped half way resumes where it stopped.
3. **Catch up again** (no lock), then `ANALYZE` the new table, which has no statistics of its own.
4. **The swap**, within `_QUIET_WAIT_S`: wait, without queuing, until no visible session has run
   a statement on `callrecord`, or kept a transaction open, for longer than `_QUIET_STATEMENT_S`
   (a lock request queues every later statement behind the one it waits for); copy what came in
   during the wait; then ask for ACCESS EXCLUSIVE on both tables with a `_LOCK_TRY` lock timeout
   inside a savepoint, and start over on a timeout, so a waiting lock request never stalls inserts
   for longer than that. It refuses if the window ends first. Holding both locks, it copies the
   last rows and resyncs the late values of the range it copied (a row it skips was copied before
   the lock), the tables and their indexes trade names, the id sequence moves to the new table
   and the old table's grants are repeated on it. Audit inserts wait for this transaction and then
   land in the new table.

Rows of teams deleted while the copy ran are removed after the swap by
`treg-worker admin callrecord-drop-legacy`, in batches that each commit, not by this revision: a
large team's rows must never make the upgrade run long after the swap committed.

An empty `callrecord` (a new database) is dropped instead of kept as `callrecord_legacy`. Failure
evidence blanked in the old table after its row was copied is blanked again by the next
`treg-worker admin purge-evidence` run, which selects by age.

A statement prepared before the swap whose result includes `callrecord.id` is refused from then
on ("cached statement plan is invalid"), because that column changed type. On a direct connection
the driver prepares it again after the first refusal; behind a transaction pooler that keeps one
server statement per query text, as the background pool may be, it stays refused until that
server connection closes. So the audit writer inserts call records without returning the id, from
the release before this one (alembic 0140 and 0141), and no background-pool query reads
`callrecord.id`.

SQLite has no partitions and its ids are 64-bit already. There the revision only drops the
foreign key to `org`, so both databases agree with the model.

Rollback floor: the build before this revision (the one that carries 0140 and 0141) runs on the
partitioned table unchanged, so a rollback is code only, to that build; never the downgrade on a
large table. The downgrade puts a plain `integer` table back under the name `callrecord` and
copies into it every row the partitioned table holds, in one statement that blocks every audit
insert while it runs; it is meant for a small database and fails with "integer out of range" once
an id has passed 2,147,483,647.
"""
from collections.abc import Sequence
from datetime import date, datetime, timedelta

import json
import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

revision: str = "0142"
down_revision: str | Sequence[str] | None = "0141"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # replaces a table; see the rollback floor note

LIVE = "callrecord"
NEW = "callrecord_partitioned"
LEGACY = "callrecord_legacy"

_INLINE_IDS = 1_000_000
# The longest look back of a reader that rebuilds from call records (the usage rollup's history).
_MIN_HISTORY_DAYS = 31
# A verdict lands at most a day after its row (an async answer is kept 24 h).
_RESYNC_LOOK_BACK = timedelta(hours=25)
_BATCH_ROWS = 20_000
# The copy repeats until at most this many ids are left, so the swap copies few rows under its lock.
_SWAP_ROWS = 500
_COPY_PASSES = 50
# Ids are handed out before their rows commit. A copy that continues above the last copied id
# starts this many ids below it, so a row that committed late is still found.
_COMMIT_MARGIN = 20_000
_MONTHS_AHEAD = 3
# More ids than this above the backfill's last resync: refuse, and ask for the backfill again.
_MAX_RESYNC_IDS = 1_000_000
# Each lock attempt waits at most this long, so it never stalls inserts for longer.
_LOCK_TRY = "50ms"
# More ids than this written since the last backfill run: refuse, and ask for the backfill again.
_MAX_CATCH_UP_IDS = 1_000_000
# The swap asks for its lock only once no statement on `callrecord` has run longer than this.
_QUIET_STATEMENT_S = 0.5
_QUIET_WAIT_S = 120.0
_QUIET_POLL_S = 0.1
_LOCK_RETRY_PAUSE_S = 0.05

_SQLITE_FK_NAME = "fk_callrecord_org_id_org"
_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}

_REFUSAL = (
    "callrecord is too large to copy during an upgrade, and callrecord_partitioned does not hold "
    "its recent history yet ({found}). Nothing was changed by this revision. Run "
    "`treg-worker admin callrecord-backfill --days N` with N of at least {days}, then upgrade again. "
    "See docs/context/architecture/data-model.md, \"monthly partitions and a 64-bit id\"."
)


def _scalar(bind, sql: str, **params):
    return bind.execute(sa.text(sql), params).scalar()


def _relkind(bind, table: str) -> str | None:
    return _scalar(bind, "SELECT relkind::text FROM pg_class WHERE oid = to_regclass(:name)", name=table)


def _columns(bind) -> str:
    names = bind.execute(sa.text(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(:name) "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"), {"name": LIVE}).scalars().all()
    return ", ".join(f'"{name}"' for name in names)


def _index_names(bind, table: str) -> list[str]:
    return list(bind.execute(sa.text(
        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE i.indrelid = to_regclass(:name) ORDER BY c.relname"), {"name": table}).scalars())


def _now(bind) -> datetime:
    return _scalar(bind, "SELECT now() AT TIME ZONE 'UTC'")


def _start(bind) -> int:
    """The id the copy continues above, or a refusal when the history is not there."""
    first, last = bind.execute(sa.text(f"SELECT min(id), max(id) FROM {LIVE}")).one()
    if first is None:
        return 0
    copied_first, copied_last = bind.execute(sa.text(f"SELECT min(id), max(id) FROM {NEW}")).one()
    if copied_first is None:
        if last - first >= _INLINE_IDS:
            raise RuntimeError(_REFUSAL.format(found="it is empty", days=_MIN_HISTORY_DAYS))
        return first - 1
    left_out = _scalar(bind, f"SELECT created_at FROM {LIVE} WHERE id < :floor ORDER BY id DESC LIMIT 1",
                       floor=copied_first)
    if left_out is not None and left_out > _now(bind) - timedelta(days=_MIN_HISTORY_DAYS):
        raise RuntimeError(_REFUSAL.format(found=f"the copy reaches back to {left_out:%Y-%m-%d} only",
                                           days=_MIN_HISTORY_DAYS))
    note = _note(bind)
    if isinstance(note.get("resync_pass"), dict):
        raise RuntimeError(
            "The last `treg-worker admin callrecord-backfill` run stopped before its late-verdict "
            "resync finished. Nothing was changed by this revision. Run the backfill again just "
            "before the deploy, then upgrade again.")
    if "resynced_upto" in note and last - int(note["resynced_upto"]) > _MAX_RESYNC_IDS:
        raise RuntimeError(
            f"callrecord has {last - int(note['resynced_upto'])} ids above the backfill's last "
            f"late-verdict resync (more than {_MAX_RESYNC_IDS}). Nothing was changed by this "
            "revision. Run `treg-worker admin callrecord-backfill --days N` again just before the "
            "deploy, then upgrade again.")
    if last - copied_last > _MAX_CATCH_UP_IDS:
        raise RuntimeError(
            f"callrecord has {last - copied_last} ids the last backfill run did not copy (more than "
            f"{_MAX_CATCH_UP_IDS}). Nothing was changed by this revision. Run "
            "`treg-worker admin callrecord-backfill --days N` again just before the deploy, then "
            "upgrade again.")
    return max(copied_last - _COMMIT_MARGIN, copied_first - 1)


def _create_partitions(bind, after: int) -> None:
    """One partition per month, from the month of the oldest row still to copy to a few ahead.
    The oldest by `created_at`, not the lowest id: an id is handed out before its row commits,
    and a row may carry an older timestamp than rows with lower ids."""
    today = _now(bind).date()
    oldest = _scalar(bind, f"SELECT min(created_at) FROM {LIVE} WHERE id > :after", after=after)
    first = min(oldest.date(), today) if oldest else today
    start = first.year * 12 + first.month - 1
    count = (today.year - first.year) * 12 + today.month - first.month + 1 + _MONTHS_AHEAD
    months = [date(index // 12, index % 12 + 1, 1) for index in range(start, start + count + 1)]
    for begin, end in zip(months, months[1:]):
        bind.execute(sa.text(
            f"CREATE TABLE IF NOT EXISTS callrecord_y{begin:%Y}m{begin:%m} PARTITION OF {NEW} "
            f"FOR VALUES FROM ('{begin}') TO ('{end}')"))


def _catch_up(bind, columns: str, reached: int) -> int:
    """Copy the ids above `reached` in batches and return the id the copy reached. Each pass
    copies what was written during the pass before it, so the passes shrink to almost nothing."""
    copy = sa.text(f"INSERT INTO {NEW} ({columns}) SELECT {columns} FROM {LIVE} "
                   "WHERE id > :after AND id <= :upto ON CONFLICT DO NOTHING")
    top = _scalar(bind, f"SELECT max(id) FROM {LIVE}") or 0
    for _ in range(_COPY_PASSES):
        for after in range(reached, top, _BATCH_ROWS):
            bind.execute(copy, {"after": after, "upto": min(after + _BATCH_ROWS, top)})
        reached = top
        top = _scalar(bind, f"SELECT max(id) FROM {LIVE}") or 0
        if top - reached <= _SWAP_ROWS:
            break
    return reached


def _first_id_at(bind, since: datetime, top: int) -> int:
    """The smallest id whose row is at or after `since`, by bisection on the primary key: ids and
    `created_at` rise together, and `callrecord` has no index on `created_at` alone."""
    low, high = 0, top + 1
    while low < high:
        mid = (low + high) // 2
        row = bind.execute(sa.text(
            f"SELECT id, created_at FROM {LIVE} WHERE id >= :mid ORDER BY id LIMIT 1"), {"mid": mid}).first()
        if row is None or row.created_at >= since:
            high = mid
        else:
            low = row.id + 1
    return low


def _note(bind) -> dict:
    """The JSON note on the new table: when the first backfill run started (written by the
    backfill), and how far a resync got (written below)."""
    raw = _scalar(bind, "SELECT obj_description(to_regclass(:name), 'pg_class')", name=NEW)
    try:
        note = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def _write_note(bind, note: dict) -> None:
    # COMMENT takes no bind parameters; the note is JSON this code wrote, quotes doubled.
    literal = json.dumps(note, sort_keys=True).replace("'", "''")
    bind.exec_driver_sql(f"COMMENT ON TABLE {NEW} IS '{literal}'")


# Bounded on both sides, so a batch probes its id range of the new table, not all of it.
_RESYNC = sa.text(
    f"UPDATE {NEW} n SET hit = o.hit, verdict = o.verdict, "
    f"cost_charged_micro = o.cost_charged_micro FROM {LIVE} o "
    "WHERE o.id > :after AND o.id <= :upto AND n.id > :after AND n.id <= :upto "
    "AND n.id = o.id AND n.created_at = o.created_at "
    "AND (n.hit IS DISTINCT FROM o.hit OR n.verdict IS DISTINCT FROM o.verdict "
    "OR n.cost_charged_micro IS DISTINCT FROM o.cost_charged_micro)")


def _resync_verdicts(bind, reached: int) -> None:
    """Late `hit`/`verdict` values for the rows above the backfill's `resynced_upto` (all rows of
    the last `_RESYNC_LOOK_BACK` when the table was copied here, with no backfill), noting the
    new `resynced_upto` after each batch."""
    note = _note(bind)
    if "resynced_upto" in note:
        after = int(note["resynced_upto"])
    else:
        after = max(_first_id_at(bind, _now(bind) - _RESYNC_LOOK_BACK, reached) - 1, 0)
    resync = _RESYNC
    while after < reached:
        upto = min(after + _BATCH_ROWS, reached)
        bind.execute(resync, {"after": after, "upto": upto})
        after = upto
        note["resynced_upto"] = after
        _write_note(bind, note)


_LONG_STATEMENTS = sa.text("""
    SELECT DISTINCT a.pid
    FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.relation = to_regclass(:name) AND l.granted AND a.pid <> pg_backend_pid()
      AND ((a.state = 'active' AND clock_timestamp() - a.query_start > make_interval(secs => :limit))
        OR (a.state LIKE 'idle in transaction%'
            AND clock_timestamp() - a.state_change > make_interval(secs => :limit))
        OR clock_timestamp() - a.xact_start > make_interval(secs => :limit))
""")


_VACUUMS = sa.text(
    "SELECT pid, phase FROM pg_stat_progress_vacuum WHERE relid = to_regclass(:name) ORDER BY pid")


def _vacuum_note(bind, table: str) -> str:
    """The refusal's hint when a VACUUM of `table` runs: an autovacuum that prevents wraparound is
    not cancelled by a waiting lock request, so the swap must wait for it to finish. Without
    `pg_read_all_stats` PostgreSQL hides the `relid` of other roles' rows, autovacuum's included,
    so no hint does not mean no vacuum: checking for one stays an operator step."""
    vacuums = bind.execute(_VACUUMS, {"name": table}).all()
    if not vacuums:
        return ""
    running = ", ".join(f"pid {pid} ({phase})" for pid, phase in vacuums)
    return (f" A VACUUM of {table} is running ({running}); let it finish (pg_stat_progress_vacuum) "
            "before upgrading again.")


def _wait_until_quiet(bind, table: str, deadline: float) -> None:
    """Return once no visible session holds a lock on `table` through a statement or a
    transaction older than `_QUIET_STATEMENT_S`; refuse at `deadline`. Sessions of other roles
    show no `query_start` without `pg_read_all_stats` and are not waited for."""
    while True:
        # Inside a transaction PostgreSQL keeps serving the first pg_stat_activity it read; without
        # this every poll would see the sessions as they were at the first one.
        bind.execute(sa.text("SELECT pg_stat_clear_snapshot()"))
        busy = bind.execute(_LONG_STATEMENTS, {"name": table, "limit": _QUIET_STATEMENT_S}).scalars().all()
        if not busy:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Refusing the callrecord swap: sessions {sorted(busy)} kept a statement on {table} "
                f"open for more than {_QUIET_STATEMENT_S} s throughout {_QUIET_WAIT_S:.0f} s; a "
                "lock request would queue every audit insert behind them. Pause the jobs that read "
                "callrecord (rollups, reports) and upgrade again." + _vacuum_note(bind, table))
        time.sleep(_QUIET_POLL_S)


def _rename(bind, table: str, to: str) -> None:
    """Rename a table and its indexes. Index names carry their table's name: `callrecord` owns
    `ix_callrecord_*`, the partitioned table before the swap `ix_callrecord_partitioned_*`, and
    the legacy table `ix_callrecord_*_legacy`."""
    for name in _index_names(bind, table):
        if name == f"{table}_pkey":
            renamed = f"{to}_pkey"
        elif to == LEGACY:
            renamed = f"{name}_legacy"
        elif table == LEGACY:
            renamed = name.removesuffix("_legacy")
        else:
            renamed = name.replace(f"ix_{table}_", f"ix_{to}_", 1)
        bind.execute(sa.text(f'ALTER INDEX "{name}" RENAME TO "{renamed}"'))
    bind.execute(sa.text(f"ALTER TABLE {table} RENAME TO {to}"))


def _repeat_grants(bind, source: str, target: str) -> None:
    grants = bind.execute(sa.text("""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
               a.privilege_type, NULL::text
        FROM pg_class c, aclexplode(c.relacl) a
        WHERE c.oid = to_regclass(:source) AND a.grantee <> c.relowner
        UNION ALL
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
               a.privilege_type, quote_ident(t.attname)
        FROM pg_attribute t, aclexplode(t.attacl) a
        WHERE t.attrelid = to_regclass(:source) AND t.attnum > 0 AND NOT t.attisdropped
    """), {"source": source}).all()
    for grantee, privilege, column in grants:
        what = f"{privilege} ({column})" if column else privilege
        bind.execute(sa.text(f"GRANT {what} ON {target} TO {grantee}"))


def _is_lock_timeout(exc: DBAPIError) -> bool:
    origin = exc.orig
    return "lock timeout" in str(origin or exc) or (
        origin is not None and "LockNotAvailableError" in type(origin).__name__)


def _try_locks(bind) -> bool:
    """ACCESS EXCLUSIVE on both tables, each request waiting at most `_LOCK_TRY`, in a savepoint
    that a timeout rolls back -> whether both are held."""
    try:
        with bind.begin_nested():
            bind.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TRY}'"))
            # The new table first: nothing else uses it, so its wait holds nobody up.
            bind.execute(sa.text(f"LOCK TABLE {NEW} IN ACCESS EXCLUSIVE MODE"))
            bind.execute(sa.text(f"LOCK TABLE {LIVE} IN ACCESS EXCLUSIVE MODE"))
    except DBAPIError as exc:
        if not _is_lock_timeout(exc):
            raise
        return False
    return True


def _copy_under_lock(bind, columns: str, reached: int) -> None:
    after = reached - _COMMIT_MARGIN
    # ON CONFLICT, not an anti-join: a row already copied costs one probe of the primary key.
    bind.execute(sa.text(
        f"INSERT INTO {NEW} ({columns}) SELECT {columns} FROM {LIVE} WHERE id > :after "
        "ON CONFLICT DO NOTHING"), {"after": after})
    # A row it skips was copied before the lock and may have got a late value since: resync the
    # same range, now that no writer can change it.
    top = _scalar(bind, f"SELECT coalesce(max(id), 0) FROM {LIVE} WHERE id > :after", after=after)
    if top > after:
        bind.execute(_RESYNC, {"after": after, "upto": top})


def _swap(bind, columns: str, reached: int) -> None:
    lock_timeout = _scalar(bind, "SHOW lock_timeout")
    deadline = time.monotonic() + _QUIET_WAIT_S
    while True:
        _wait_until_quiet(bind, LIVE, deadline)
        # What came in during the wait is copied now, so the locked copy stays small, and late
        # values that landed meanwhile on rows copied after the first resync are taken too.
        reached = _catch_up(bind, columns, reached)
        _resync_verdicts(bind, reached)
        if _try_locks(bind):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Refusing the callrecord swap: its lock was not granted within {_QUIET_WAIT_S:.0f} s "
                f"of {_LOCK_TRY} attempts. Pause the jobs that read callrecord and upgrade again."
                + _vacuum_note(bind, LIVE))
        time.sleep(_LOCK_RETRY_PAUSE_S)
    bind.execute(sa.text("SELECT set_config('lock_timeout', :value, true)"), {"value": lock_timeout})
    _copy_under_lock(bind, columns, reached)
    sequence = _scalar(bind, f"SELECT pg_get_serial_sequence('{LIVE}', 'id')")
    empty = _scalar(bind, f"SELECT NOT EXISTS (SELECT 1 FROM {LIVE})")
    _repeat_grants(bind, LIVE, NEW)
    bind.execute(sa.text(f"COMMENT ON TABLE {NEW} IS NULL"))
    _rename(bind, LIVE, LEGACY)
    _rename(bind, NEW, LIVE)
    bind.execute(sa.text(f"ALTER SEQUENCE {sequence} OWNED BY {LIVE}.id"))
    if empty:
        bind.execute(sa.text(f"DROP TABLE {LEGACY}"))
    else:
        bind.execute(sa.text(f"ALTER TABLE {LEGACY} ALTER COLUMN id DROP DEFAULT"))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with op.batch_alter_table(LIVE, naming_convention=_NAMING_CONVENTION) as batch:
            batch.drop_constraint(_SQLITE_FK_NAME, type_="foreignkey")
        return
    if _relkind(bind, LIVE) != "p":
        columns = _columns(bind)
        with op.get_context().autocommit_block():
            start = _start(bind)
            _create_partitions(bind, start)
            reached = _catch_up(bind, columns, start)
            _resync_verdicts(bind, reached)
            # The resync takes as long as it takes; what was written meanwhile is copied here,
            # without a lock, not by the swap under it.
            reached = _catch_up(bind, columns, reached)
            # The new table holds weeks of rows: a longer limit for this statement only.
            statement_timeout = _scalar(bind, "SHOW statement_timeout")
            bind.execute(sa.text("SET statement_timeout = '15min'"))
            try:
                bind.execute(sa.text(f"ANALYZE {NEW}"))
            finally:
                bind.execute(sa.text("SELECT set_config('statement_timeout', :value, false)"),
                             {"value": statement_timeout})
        _swap(op.get_bind(), columns, reached)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with op.batch_alter_table(LIVE, naming_convention=_NAMING_CONVENTION) as batch:
            batch.create_foreign_key(_SQLITE_FK_NAME, "org", ["org_id"], ["id"])
        return
    if _relkind(bind, LIVE) != "p":
        return
    columns = _columns(bind)
    sequence = _scalar(bind, f"SELECT pg_get_serial_sequence('{LIVE}', 'id')")
    bind.execute(sa.text(f"LOCK TABLE {LIVE} IN ACCESS EXCLUSIVE MODE"))
    if _relkind(bind, LEGACY) is None:
        bind.execute(sa.text(f"CREATE TABLE {LEGACY} (LIKE {LIVE} INCLUDING DEFAULTS)"))
        bind.execute(sa.text(f"ALTER TABLE {LEGACY} ALTER COLUMN id TYPE integer"))
        bind.execute(sa.text(f"ALTER TABLE {LEGACY} ADD CONSTRAINT {LEGACY}_pkey PRIMARY KEY (id)"))
        bind.execute(sa.text(f"ALTER TABLE {LEGACY} ADD CONSTRAINT callrecord_org_id_fkey "
                             "FOREIGN KEY (org_id) REFERENCES org (id)"))
        for name in _index_names(bind, LIVE):
            if name != f"{LIVE}_pkey":
                definition = _scalar(bind, "SELECT pg_get_indexdef(to_regclass(:name))", name=name)
                bind.execute(sa.text(
                    definition.replace(f"INDEX {name} ON ONLY ", f"INDEX {name}_legacy ON ", 1)
                    .replace(f".{LIVE} ", f".{LEGACY} ", 1)))
    bind.execute(sa.text(
        f"INSERT INTO {LEGACY} ({columns}) SELECT {columns} FROM {LIVE} c "
        f"WHERE NOT EXISTS (SELECT 1 FROM {LEGACY} l WHERE l.id = c.id)"))
    _repeat_grants(bind, LIVE, LEGACY)
    _rename(bind, LIVE, NEW)
    _rename(bind, LEGACY, LIVE)
    bind.execute(sa.text(f"ALTER TABLE {LIVE} ALTER COLUMN id SET DEFAULT nextval('{sequence}')"))
    bind.execute(sa.text(f"ALTER SEQUENCE {sequence} OWNED BY {LIVE}.id"))
