"""partial index on idempotentcall (expires_at) WHERE archive_content_hash IS NOT NULL - the archive
pruner reads which stored answers live retry rows still point to

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-05

A retry row that dropped its own copy (0065) names an archive answer, and `archive.prune_once` must
not strip those bytes while the row is inside its 24-hour window. The pruner asks once per pass for
the content hashes of unexpired trimmed rows; this partial index holds only trimmed rows, so that
question reads about a day of them instead of the whole table.

Built with the 0053 discipline (CONCURRENTLY in an autocommit block, INVALID debris dropped first).
The expand-safety linter counts the autocommit escape as non-additive, so this revision declares a
rollback floor pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | Sequence[str] | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "idempotentcall"
_NAME = "ix_idempotentcall_archive_live"
_WHERE = sa.text("archive_content_hash IS NOT NULL")

_LOCK_TIMEOUT = "180s"
_STATEMENT_TIMEOUT = "600s"
_ENV_LOCK_TIMEOUT = "5s"
_ENV_STATEMENT_TIMEOUT = "120s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(_NAME, _TABLE, ["expires_at"], sqlite_where=_WHERE)
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
                op.create_index(_NAME, _TABLE, ["expires_at"], postgresql_where=_WHERE,
                                postgresql_concurrently=True)
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    op.drop_index(_NAME, table_name=_TABLE)
