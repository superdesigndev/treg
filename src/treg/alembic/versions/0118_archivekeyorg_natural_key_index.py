"""archivekeyorg: the unique index that 0119 turns into the primary key

Revision ID: 0118
Revises: 0117
Create Date: 2026-11-18

0119 makes (org_id, key_hash) the primary key of `archivekeyorg`, so a first paid mark no longer
takes a number from the table's 32-bit serial sequence inside the settle transaction. PostgreSQL
can attach an existing unique index as a primary key without a scan (`ADD PRIMARY KEY USING
INDEX`), but not an index a constraint already owns, and `uq_archive_key_org` is a constraint's
index. This revision makes sure a second unique index on the same columns exists for 0119.

A large table's index is built by hand before the deploy, never by this revision: a concurrent
build of it can outlast a deploy's time limit, and one stopped half way leaves an INVALID index
that the next deploy would have to rebuild from the start. So this revision:

- leaves a valid index of that name alone (the operator built it);
- builds it itself (CREATE UNIQUE INDEX CONCURRENTLY, outside a transaction, no statement timeout
  for that statement only) when the table holds at most `BUILD_IN_MIGRATION_MAX_ROWS` rows,
  checked with a bounded count that reads no more than that; a new or small install needs no
  manual step;
- otherwise refuses at once with `MANUAL_BUILD`, the statements to run by hand. An INVALID index
  left by an interrupted build is dropped and rebuilt only on a small table; on a large one the
  refusal's statements drop it first.

A lock timeout during the small build (a concurrent build waits for every older snapshot in the
database) is raised as a plain error, not as the database error `maintenance` retries: a retry
would start the build again from zero. SQLite: nothing to do, 0119 rebuilds the table there.

Rollback floor (pro forma, like 0072): the expand-safety linter counts the autocommit block and
the invalid-index drop as non-additive. Older code ignores the extra index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

revision: str = "0118"
down_revision: str | Sequence[str] | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma, see the rollback floor note: the operation adds an index

NAME = "ix_archivekeyorg_org_key_unique"
BUILD_IN_MIGRATION_MAX_ROWS = 1_000_000
BUILD_LOCK_TIMEOUT = "180s"

_MANUAL_STEPS = (
    "From a session with no statement timeout (psql), then deploy again:\n"
    "  SET statement_timeout = 0;\n"
    f"  DROP INDEX CONCURRENTLY IF EXISTS {NAME};\n"
    f"  CREATE UNIQUE INDEX CONCURRENTLY {NAME} ON archivekeyorg (org_id, key_hash);\n"
    f"  SELECT indisvalid FROM pg_index WHERE indexrelid = '{NAME}'::regclass;  -- must be true")

MANUAL_BUILD = (
    f"Refusing migration 0118: archivekeyorg holds more than {BUILD_IN_MIGRATION_MAX_ROWS} rows and "
    f"the unique index {NAME} is missing or INVALID. Build it by hand before deploying. "
    + _MANUAL_STEPS)

_STATE = sa.text("""
    SELECT ix.indisvalid AS valid,
           t.relname = 'archivekeyorg' AND ix.indisunique AND ix.indnatts = 2
           AND ix.indkey[0] = (SELECT attnum FROM pg_attribute
                               WHERE attrelid = t.oid AND attname = 'org_id')
           AND ix.indkey[1] = (SELECT attnum FROM pg_attribute
                               WHERE attrelid = t.oid AND attname = 'key_hash')
           AND ix.indexprs IS NULL AND ix.indpred IS NULL AND am.amname = 'btree' AS expected
    FROM pg_class i
    JOIN pg_namespace n ON n.oid = i.relnamespace
    JOIN pg_index ix ON ix.indexrelid = i.oid
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_am am ON am.oid = i.relam
    WHERE n.nspname = current_schema() AND i.relname = :name
""")

_PRIMARY_KEY_IS_NATURAL = sa.text("""
    SELECT array_agg(a.attname::text ORDER BY k.ord) = ARRAY['org_id', 'key_hash']
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
    WHERE n.nspname = current_schema() AND t.relname = 'archivekeyorg' AND c.contype = 'p'
""")

_BOUNDED_COUNT = sa.text(
    "SELECT count(*) FROM (SELECT 1 FROM archivekeyorg LIMIT :limit) AS bounded")


def _is_lock_timeout(exc: DBAPIError) -> bool:
    origin = exc.orig
    return "lock timeout" in str(origin or exc) or (
        origin is not None and "LockNotAvailableError" in type(origin).__name__)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(_PRIMARY_KEY_IS_NATURAL).scalar():
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        state = bind.execute(_STATE, {"name": NAME}).mappings().one_or_none()
        if state is not None and state["expected"] is not True:
            raise RuntimeError(
                f"Refusing index migration: {NAME} exists but is not unique archivekeyorg(org_id, key_hash)")
        if state is not None and state["valid"]:
            return
        rows = bind.execute(_BOUNDED_COUNT, {"limit": BUILD_IN_MIGRATION_MAX_ROWS + 1}).scalar_one()
        if rows > BUILD_IN_MIGRATION_MAX_ROWS:
            raise RuntimeError(MANUAL_BUILD)
        settings = {name: bind.execute(sa.text(f"SHOW {name}")).scalar_one()
                    for name in ("lock_timeout", "statement_timeout")}
        try:
            bind.execute(sa.text(f"SET lock_timeout = '{BUILD_LOCK_TIMEOUT}'"))
            bind.execute(sa.text("SET statement_timeout = 0"))   # this build only; restored below
            if state is not None:   # left INVALID by an interrupted concurrent build
                op.drop_index(NAME, table_name="archivekeyorg", postgresql_concurrently=True,
                              if_exists=True)
            bind.execute(sa.text(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {NAME} "
                "ON archivekeyorg (org_id, key_hash)"))
        except DBAPIError as exc:
            if not _is_lock_timeout(exc):
                raise
            raise RuntimeError(
                f"Migration 0118 gave up building {NAME}: a transaction open longer than the lock "
                "timeout held the build back. End that transaction and deploy again, or build the "
                "index by hand. " + _MANUAL_STEPS) from exc
        finally:
            for name, value in settings.items():
                bind.execute(sa.text("SELECT set_config(:name, :value, false)"),
                             {"name": name, "value": value})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.drop_index(NAME, table_name="archivekeyorg", postgresql_concurrently=True, if_exists=True)
