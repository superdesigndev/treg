"""composite (membership_id, expires_at) on idempotentcall - the per-call expired-label sweep stops
reading every label the caller ever stored

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-29

`_claim_idempotent` runs `DELETE ... WHERE membership_id = ? AND expires_at < now` on EVERY call
that sends an Idempotency-Key, inside the intake transaction on an api-pool connection. The table
carried `membership_id` alone (plus the unique `(membership_id, key)`), so the delete walked every
row the caller holds - a stored response body per paid call - to find the handful past their
window. A batch caller holds tens of thousands of live labels, and the sweep read all of them per
call: 88 ms mean over 3M calls in `pg_stat_statements`, on the same 1 vCPU database whose CPU pinned
during the saturation windows that stranded idempotency claims and settlements.

`(membership_id, expires_at)` turns the sweep into a range over only the expired rows. The existing
single-column index stays: dropping it is a separate, non-additive change.

Built with the 0020/0021 discipline (CONCURRENTLY in an autocommit block, INVALID debris dropped
first). The expand-safety linter counts the autocommit escape as non-additive, so this revision
declares a rollback floor pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | Sequence[str] | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "idempotentcall"
_INDEXES = (
    ("ix_idempotentcall_membership_id_expires_at", ["membership_id", "expires_at"]),
)

# Same values and reasoning as 0021; `env.py`'s values are restored before the block ends.
_LOCK_TIMEOUT = "180s"
_STATEMENT_TIMEOUT = "600s"
_ENV_LOCK_TIMEOUT = "5s"
_ENV_STATEMENT_TIMEOUT = "120s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        for name, columns in _INDEXES:  # SQLite: no concurrent mode, no traffic to block
            op.create_index(name, _TABLE, columns)
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'"))
        bind.execute(sa.text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        try:
            for name, columns in _INDEXES:
                valid = bind.execute(_VALIDITY, {"name": name}).scalar()
                if valid is True:
                    continue
                if valid is False:  # debris from a killed build - unusable, and never repaired
                    op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True)
                op.create_index(name, _TABLE, columns, postgresql_concurrently=True)
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    for name, _ in _INDEXES:
        op.drop_index(name, table_name=_TABLE)
