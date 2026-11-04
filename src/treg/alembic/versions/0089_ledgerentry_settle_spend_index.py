"""partial covering (org_id, created_at) INCLUDE (amount_micro) WHERE kind = 'settle' on
ledgerentry - a team's spend over a window is read from the index alone

Revision ID: 0089
Revises: 0088
Create Date: 2026-10-28

`money.spend_since` (the Usage page's total and `/usage/by-tag`'s team total) sums
`amount_micro` over `org_id = ? AND kind = 'settle' AND created_at >= ?` for up to a year.
`(org_id, created_at)` finds the range, but neither `kind` nor `amount_micro` is in it, so every
row in the window, settle or not, costs a heap fetch. The table is append-only and a busy team's
rows are spread over the whole window, so on a cold cache that is one random page read per row and
the report runs past its statement timeout.

A partial index over the settle rows carrying `amount_micro` turns it into an index-only scan of
just the rows it sums; append-only rows are all-visible once vacuumed, so the heap is barely
touched. Partial rather than `(org_id, kind, created_at)`: it is about half the size, and every
index on this write-heavy table is paid on every write. Postgres matches a partial index only
when the query states the predicate as a constant, so `spend_since` renders `'settle'` inline.

Built with the 0020/0021 discipline (CONCURRENTLY in an autocommit block, INVALID debris dropped
first, an existing valid index left alone). On a ledger large enough that the build outlasts
`_STATEMENT_TIMEOUT`, build the same index out of band with `CREATE INDEX CONCURRENTLY` before
deploying; this revision then finds it valid and only records the revision. The expand-safety
linter counts the autocommit escape as non-additive, so this revision declares a rollback floor
pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089"
down_revision: str | Sequence[str] | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "ledgerentry"
_NAME = "ix_ledgerentry_settle_org_id_created_at"
_COLUMNS = ["org_id", "created_at"]
_SETTLE = "kind = 'settle'"

# Same values and reasoning as 0021; `env.py`'s values are restored before the block ends.
_LOCK_TIMEOUT = "180s"
_STATEMENT_TIMEOUT = "600s"
_ENV_LOCK_TIMEOUT = "5s"
_ENV_STATEMENT_TIMEOUT = "120s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":  # SQLite: no concurrent mode, no INCLUDE
        op.create_index(_NAME, _TABLE, _COLUMNS, sqlite_where=sa.text(_SETTLE))
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
                                postgresql_include=["amount_micro"],
                                postgresql_where=sa.text(_SETTLE))
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    op.drop_index(_NAME, table_name=_TABLE)
