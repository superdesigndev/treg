"""autotopup_monthly_cap_micro becomes BIGINT to support caps above ~$2,147

Revision ID: 0103
Revises: 0102
Create Date: 2026-11-13

The monthly cap for auto top-up can be set much higher than a single top-up amount (up to $100k),
so the column must be BIGINT to hold values above $2,147.48 (the integer max in micro-USD).
This was missed in revision 0093 which converted other money running totals.

On Postgres the change runs in one ALTER statement. SQLite uses batch mode because it rewrites
the whole table. The column already existed; this is a type change only.

Rollback is safe while every stored value fits in integer.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0103"
down_revision: str | Sequence[str] | None = "0102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        types = dict(bind.execute(sa.text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'org' "
            "AND column_name = 'autotopup_monthly_cap_micro'"
        )).all())
        if types.get("autotopup_monthly_cap_micro") == "bigint":
            return
        with op.get_context().autocommit_block():
            bind.execute(sa.text(
                "ALTER TABLE org ALTER COLUMN autotopup_monthly_cap_micro TYPE BIGINT"
            ))
    else:
        with op.batch_alter_table("org") as batch:
            batch.alter_column(
                "autotopup_monthly_cap_micro",
                existing_type=sa.Integer(),
                type_=sa.BigInteger(),
                existing_nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        types = dict(bind.execute(sa.text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'org' "
            "AND column_name = 'autotopup_monthly_cap_micro'"
        )).all())
        if types.get("autotopup_monthly_cap_micro") == "integer":
            return
        with op.get_context().autocommit_block():
            bind.execute(sa.text(
                "ALTER TABLE org ALTER COLUMN autotopup_monthly_cap_micro TYPE INTEGER"
            ))
    else:
        with op.batch_alter_table("org") as batch:
            batch.alter_column(
                "autotopup_monthly_cap_micro",
                existing_type=sa.BigInteger(),
                type_=sa.Integer(),
                existing_nullable=False,
            )
