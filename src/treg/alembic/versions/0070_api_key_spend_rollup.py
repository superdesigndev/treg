"""spend per API key: a daily rollup of the ledger's settle entries and the cursor that fills it

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-12

Additive: two new small tables, nothing else touched. The ledger and `callrecord` are only read
by the worker that fills them (`treg-worker spend rollup`), through indexes they already have.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0070"
down_revision: str | Sequence[str] | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apikeyspendday",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("day", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("spent_micro", sa.BigInteger(), nullable=False),
        sa.Column("charges", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("org_id", "day", "api_key_id", "endpoint_id"),
    )
    op.create_table(
        "apikeyspendcursor",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("watermark_at", sa.DateTime(), nullable=False),
        sa.Column("watermark_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("history_from", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("caught_up_at", sa.DateTime(), nullable=True),
        sa.Column("recounted_on", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("recount_day", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("recount_after_org", sa.Integer(), nullable=False),
        sa.Column("recount_owner", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("recount_lease_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("apikeyspendcursor")
    op.drop_table("apikeyspendday")
