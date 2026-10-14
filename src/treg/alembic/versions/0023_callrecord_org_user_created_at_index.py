"""composite (org_id, user_email, created_at) on callrecord — the per-user daily cap stops reading
a member's whole history

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-06

`governance/usage.count_today` backs the per-user daily call cap and runs on every capped call:
`count(*) WHERE org_id = ? AND user_email = ? AND created_at >= today`. With 0020's
`(org_id, created_at)` and the single-column `ix_callrecord_user_email` the planner BitmapAnd-ed
the two, and the `user_email` half read the member's WHOLE history. Measured on prod 2026-09-06,
after 0021, for the busiest member (287k rows, 104k of them today):

    Bitmap Index Scan on ix_callrecord_user_email   rows=287,527   2,621 ms of 3,016 ms
    seen in the activity sample 96 times, longest 45.8 s cold

The triple is one tight range: this org, this member, since midnight. Built with the 0020
discipline (raised `lock_timeout` for the CONCURRENT build only, INVALID-debris check, `env.py`
timeouts restored) — see that revision for why waiting on this lock blocks nobody. The expand-safety
linter counts the autocommit escape as non-additive, so this revision declares a rollback floor
pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma — see the rollback floor note; the operation is one additive index

_TABLE = "callrecord"
_INDEXES = (
    ("ix_callrecord_org_id_user_email_created_at", ["org_id", "user_email", "created_at"]),
)

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
                if valid is False:  # debris from a killed build — unusable, and never repaired
                    op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True)
                op.create_index(name, _TABLE, columns, postgresql_concurrently=True)
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    for name, _ in _INDEXES:
        op.drop_index(name, table_name=_TABLE)
