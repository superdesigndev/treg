"""money running totals become BIGINT: a team's balance can pass $2,147.48

Revision ID: 0093
Revises: 0092
Create Date: 2026-11-03

Micro-USD in a Postgres `integer` stops at 2,147,483,647, which is $2,147.48. A top-up or a grant
that would take `org.balance_micro` past it failed with "integer out of range", so the Stripe
webhook returned 500 and the team was not credited. Single amounts are bounded far below that
(`billing.TOPUP_MAX_USD`); running totals and caps are not, so only those columns move:

- `org`: `balance_micro`, `daily_cap_micro`, `autotopup_amount_micro`, `autotopup_threshold_micro`
- `creditblock`: `amount_micro`, `remaining_micro`
- `hold`: `amount_micro`
- `tagbudget`: `daily_cap_micro`, `monthly_cap_micro`

These tables are small, so the rewrite takes seconds. The append-only journals (`ledgerentry`,
`callrecord`, `tagspend`, `idempotentcall`, `asynctaskrecord`) stay `integer`: each of their rows
holds one call's or one payment's amount, and a type change there rewrites the whole table under
an ACCESS EXCLUSIVE lock for hours, which stops every paid call.

On Postgres each table changes in one `ALTER TABLE` statement, so it is rewritten once, not once
per column, and each statement commits on its own. Live calls touch `org` and `creditblock` in
one transaction; a migration that held `org` while it waited for `creditblock` deadlocked with
them. One table per transaction holds only one table's lock at a time, so it cannot. A table that
already has the target type is skipped, so a retry after a lock timeout resumes where it stopped.
`env.py`'s session `lock_timeout` makes each statement fail fast under contention.

Rollback floor: code older than this revision runs on BIGINT columns unchanged, so a code rollback
does not need the downgrade. The downgrade works only while every value still fits in `integer`;
once a balance passes $2,147.48 it fails with "integer out of range" and changes nothing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0093"
down_revision: str | Sequence[str] | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # column type change; see the rollback floor note

_COLUMNS: dict[str, dict[str, bool]] = {  # table -> column -> nullable
    "org": {"balance_micro": False, "daily_cap_micro": False,
            "autotopup_amount_micro": False, "autotopup_threshold_micro": False},
    "creditblock": {"amount_micro": False, "remaining_micro": False},
    "hold": {"amount_micro": False},
    "tagbudget": {"daily_cap_micro": True, "monthly_cap_micro": True},
}

_TYPES = sa.text(
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = :table")


def _retype(old: type[sa.types.TypeEngine], new: type[sa.types.TypeEngine], sql_type: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for table, columns in _COLUMNS.items():
                types = dict(bind.execute(_TYPES, {"table": table}).all())
                pending = [name for name in columns if types.get(name) != sql_type.lower()]
                if not pending:
                    continue
                clauses = ", ".join(f"ALTER COLUMN {name} TYPE {sql_type}" for name in pending)
                bind.execute(sa.text(f"ALTER TABLE {table} {clauses}"))
        return
    for table, columns in _COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            for name, nullable in columns.items():
                batch.alter_column(name, existing_type=old(), type_=new(), existing_nullable=nullable)


def upgrade() -> None:
    _retype(sa.Integer, sa.BigInteger, "BIGINT")


def downgrade() -> None:
    _retype(sa.BigInteger, sa.Integer, "INTEGER")
