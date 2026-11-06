"""partial (org_id, created_at) WHERE kind = 'grant' on ledgerentry - a team's grants are found
without walking all of its ledger rows

Revision ID: 0095
Revises: 0094
Create Date: 2026-11-04

The payments page reads a team's grant entries (`application/billing.py`, the bonus each top-up
earned) with `org_id = ? AND kind = 'grant'`. As with top-ups before 0090, no index leads with both,
so Postgres intersects the `org_id` and `kind` bitmaps and reads every one of the team's ledger
rows to find its few grants.

The same shape as 0090: a partial index over the grant rows only, matched because the billing read
renders `'grant'` inline. Built the same way, including the out-of-band `CREATE INDEX CONCURRENTLY`
route on a ledger too large to scan within `_STATEMENT_TIMEOUT`, and the pro forma rollback floor.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0095"
down_revision: str | Sequence[str] | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "ledgerentry"
_NAME = "ix_ledgerentry_grant_org_id_created_at"
_COLUMNS = ["org_id", "created_at"]
_GRANT = "kind = 'grant'"

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
        op.create_index(_NAME, _TABLE, _COLUMNS, sqlite_where=sa.text(_GRANT))
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
                                postgresql_where=sa.text(_GRANT))
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    op.drop_index(_NAME, table_name=_TABLE)
