"""governance: per-team, per-day admitted-call counter for catalog endpoints on treg's key

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-17

Additive: one new small table, nothing else touched. One row per (team, endpoint, UTC day) that
made at least one admitted call to an allowance-bearing catalog endpoint on treg's key; the reservation gate
increments it with a conditional upsert (`domain.governance.allowance`).
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "endpointallowance",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("day", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("org_id", "endpoint_id", "day"),
    )
    op.create_index("ix_endpointallowance_day", "endpointallowance", ["day"])


def downgrade() -> None:
    op.drop_index("ix_endpointallowance_day", table_name="endpointallowance")
    op.drop_table("endpointallowance")
