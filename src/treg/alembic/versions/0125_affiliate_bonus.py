"""affiliatebonus - the partner offer's bonus credit owed on a paid top-up of an affiliate's team

Revision ID: 0125
Revises: 0124
Create Date: 2026-11-23

A new outbox table, one row per paid top-up of a team a partner brought. Additive: no existing row
changes. No foreign key: team deletion removes the row in code (`ORG_SCOPED_MODELS`).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0125"
down_revision: str | Sequence[str] | None = "0124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "affiliatebonus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("payment_intent", sa.String(), nullable=False),
        sa.Column("topup_micro", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("offer", sa.String(), nullable=False),
        sa.Column("bonus_micro", sa.BigInteger(), nullable=False),
        sa.Column("block_id", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=False),
        sa.UniqueConstraint("payment_intent", name="uq_affiliatebonus_payment"),
    )
    op.create_index("ix_affiliatebonus_org_id", "affiliatebonus", ["org_id"])
    op.create_index("ix_affiliatebonus_next_attempt_at", "affiliatebonus", ["next_attempt_at"])
    op.create_index("ix_affiliatebonus_decided_at", "affiliatebonus", ["decided_at"])


def downgrade() -> None:
    op.drop_table("affiliatebonus")
