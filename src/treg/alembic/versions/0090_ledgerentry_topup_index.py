"""partial (org_id, created_at) WHERE kind = 'topup' on ledgerentry - a team's top-ups are found
without walking all of its ledger rows

Revision ID: 0090
Revises: 0089
Create Date: 2026-10-29

The top-up reads in `application/billing.py` (the month's automatic top-ups behind the auto top-up
cap, the last manual top-up behind the next default amount, the payments page's auto flags, the
invoice recovery boundary) all filter `org_id = ? AND kind = 'topup'`. No index leads with both, so
Postgres intersects the `org_id` and `kind` bitmaps: every one of the team's rows is read to find
the handful that are top-ups, on a path that runs for every automatic top-up check.

A partial index over the top-up rows holds only those few entries, so each read touches a few index
pages. Postgres matches a partial index only when the query states the predicate as a constant, so
the billing reads render `'topup'` inline.

Built like 0089. The index is small, but building it still scans the whole table; on a ledger large
enough that the scan outlasts `_STATEMENT_TIMEOUT`, build it out of band with `CREATE INDEX
CONCURRENTLY` before deploying, and this revision finds it valid and only records the revision. The
expand-safety linter counts the autocommit escape as non-additive, so this revision declares a
rollback floor pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0090"
down_revision: str | Sequence[str] | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "ledgerentry"
_NAME = "ix_ledgerentry_topup_org_id_created_at"
_COLUMNS = ["org_id", "created_at"]
_TOPUP = "kind = 'topup'"

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
        op.create_index(_NAME, _TABLE, _COLUMNS, sqlite_where=sa.text(_TOPUP))
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
                                postgresql_where=sa.text(_TOPUP))
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    op.drop_index(_NAME, table_name=_TABLE)
