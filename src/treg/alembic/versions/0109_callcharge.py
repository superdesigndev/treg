"""call charges: what a call cost, queued until it is applied to the credit blocks

Revision ID: 0109
Revises: 0108
Create Date: 2026-11-17

Additive: one new table, nothing else touched, and nothing reads or writes it yet. A row lives
only until the transaction that applies it to the team's blocks, which deletes it, so the table
holds the calls not yet applied and never a history. No foreign keys (AGENTS.md); team deletion
clears its rows through ORG_SCOPED_MODELS. Amounts are BIGINT micro-USD.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0109"
down_revision: str | Sequence[str] | None = "0108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "callcharge",
        sa.Column("call_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("call_id"),
    )
    op.create_index("ix_callcharge_org_id", "callcharge", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_callcharge_org_id", table_name="callcharge")
    op.drop_table("callcharge")
