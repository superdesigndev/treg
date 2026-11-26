"""partial index on idempotentcall (archive_key_hash) WHERE archive_content_hash IS NOT NULL - the
archive row-retention job asks whether a live retry row still replays from a key

Revision ID: 0123
Revises: 0122
Create Date: 2026-11-18

`treg-worker archive retain` keeps a key an unexpired trimmed retry row replays from, and asks
that per candidate key twice per batch (the candidate read and the locked re-check). Without an
index on `archive_key_hash` each ask is a scan of the whole table. This partial index holds only
trimmed rows, so it stays small.

A large table's index is built by hand before the deploy, never by this revision: a concurrent
build reads the whole table and can outlast a deploy's time limit, and one stopped half way
leaves an INVALID index the next deploy would rebuild from the start. So this revision leaves a
valid index alone, builds it itself (CONCURRENTLY, in an autocommit block, INVALID debris dropped
first) only when the table holds at most `BUILD_IN_MIGRATION_MAX_ROWS` rows (a bounded count),
and otherwise refuses at once with `MANUAL_BUILD`, the statements to run by hand. A lock timeout
during the small build is raised as a plain error, so the upgrade's lock-timeout retry does not
start the build again from zero.

The expand-safety linter counts the autocommit escape as non-additive, so this revision declares a
rollback floor pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

revision: str = "0123"
down_revision: str | Sequence[str] | None = "0122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "idempotentcall"
NAME = "ix_idempotentcall_archive_key_hash"
_WHERE = sa.text("archive_content_hash IS NOT NULL")
BUILD_IN_MIGRATION_MAX_ROWS = 1_000_000
BUILD_LOCK_TIMEOUT = "180s"

_MANUAL_STEPS = (
    "From a session with no statement timeout (psql), then deploy again:\n"
    "  SET statement_timeout = 0;\n"
    f"  DROP INDEX CONCURRENTLY IF EXISTS {NAME};\n"
    f"  CREATE INDEX CONCURRENTLY {NAME} ON {_TABLE} (archive_key_hash) "
    "WHERE archive_content_hash IS NOT NULL;\n"
    f"  SELECT indisvalid FROM pg_index WHERE indexrelid = '{NAME}'::regclass;  -- must be true")

MANUAL_BUILD = (
    f"Refusing migration 0123: {_TABLE} holds more than {BUILD_IN_MIGRATION_MAX_ROWS} rows and the "
    f"index {NAME} is missing or INVALID. Build it by hand before deploying. " + _MANUAL_STEPS)

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = :name")

_BOUNDED_COUNT = sa.text(
    f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} LIMIT :limit) AS bounded")


def _is_lock_timeout(exc: DBAPIError) -> bool:
    origin = exc.orig
    return "lock timeout" in str(origin or exc) or (
        origin is not None and "LockNotAvailableError" in type(origin).__name__)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(NAME, _TABLE, ["archive_key_hash"], sqlite_where=_WHERE)
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        valid = bind.execute(_VALIDITY, {"name": NAME}).scalar()
        if valid is True:
            return
        rows = bind.execute(_BOUNDED_COUNT, {"limit": BUILD_IN_MIGRATION_MAX_ROWS + 1}).scalar_one()
        if rows > BUILD_IN_MIGRATION_MAX_ROWS:
            raise RuntimeError(MANUAL_BUILD)
        settings = {name: bind.execute(sa.text(f"SHOW {name}")).scalar_one()
                    for name in ("lock_timeout", "statement_timeout")}
        try:
            bind.execute(sa.text(f"SET lock_timeout = '{BUILD_LOCK_TIMEOUT}'"))
            bind.execute(sa.text("SET statement_timeout = 0"))   # this build only; restored below
            if valid is False:  # debris from a killed build - unusable, and never repaired
                op.drop_index(NAME, table_name=_TABLE, postgresql_concurrently=True)
            op.create_index(NAME, _TABLE, ["archive_key_hash"], postgresql_where=_WHERE,
                            postgresql_concurrently=True)
        except DBAPIError as exc:
            if not _is_lock_timeout(exc):
                raise
            raise RuntimeError(
                f"Migration 0123 gave up building {NAME}: a transaction open longer than the lock "
                "timeout held the build back. End that transaction and deploy again, or build the "
                "index by hand. " + _MANUAL_STEPS) from exc
        finally:
            for name, value in settings.items():
                bind.execute(sa.text("SELECT set_config(:name, :value, false)"),
                             {"name": name, "value": value})


def downgrade() -> None:
    op.drop_index(NAME, table_name=_TABLE)
