"""partial (status_code, id) over the rows with failure evidence on callrecord - the errors report
finds a rare status without walking the window

Revision ID: 0093
Revises: 0092
Create Date: 2026-11-01

`GET /admin/errors` with a status filter reads the newest rows of that status that carry failure
evidence. No index leads with `status_code`, so the report walks the primary key newest-first
through the whole window until the page is full; a status that is rare in the window (a provider's
503s against its 502s) walks most of the window and passes the report's statement timeout, which
the report answers as busy.

A partial index over the rows that hold evidence, keyed by status and id, holds only the failed
calls and serves the report's shape directly: the status, newest first, filtered by provider. The
predicate is the report's own evidence clause, stated the same way, so Postgres matches it.

Built like 0090 and 0089. The index is small next to the table, but building it still scans the
whole table; where that scan outlasts `_STATEMENT_TIMEOUT`, build it out of band with `CREATE
INDEX CONCURRENTLY` before deploying, and this revision finds it valid and only records the
revision. The expand-safety linter counts the autocommit escape as non-additive, so this revision
declares a rollback floor pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0093"
down_revision: str | Sequence[str] | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "callrecord"
_NAME = "ix_callrecord_error_status_code_id"
_COLUMNS = ["status_code", "id"]
_EVIDENCE = "error_request IS NOT NULL OR error_response IS NOT NULL"

# Same values and reasoning as 0021; `env.py`'s values are restored before the block ends.
_LOCK_TIMEOUT = "180s"
_STATEMENT_TIMEOUT = "600s"
_ENV_LOCK_TIMEOUT = "5s"
_ENV_STATEMENT_TIMEOUT = "120s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":  # SQLite: no concurrent mode
        op.create_index(_NAME, _TABLE, _COLUMNS, sqlite_where=sa.text(_EVIDENCE))
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'"))
        bind.execute(sa.text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        try:
            valid = bind.execute(_VALIDITY, {"name": _NAME}).scalar()
            if valid is not True:
                if valid is False:  # debris from a killed build - unusable, and never repaired
                    op.drop_index(_NAME, table_name=_TABLE, postgresql_concurrently=True)
                op.create_index(_NAME, _TABLE, _COLUMNS, postgresql_concurrently=True,
                                postgresql_where=sa.text(_EVIDENCE))
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    op.drop_index(_NAME, table_name=_TABLE)
