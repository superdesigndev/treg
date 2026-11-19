"""affiliatelead - the Dub lead owed for a team that arrived through an affiliate link

Revision ID: 0118
Revises: 0117
Create Date: 2026-11-19

A new outbox table, one row per attributed team. Additive: no existing row changes.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0118"
down_revision: str | Sequence[str] | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "affiliatelead",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("click_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_affiliatelead_org"),
    )
    op.create_index("ix_affiliatelead_user_id", "affiliatelead", ["user_id"])
    op.create_index("ix_affiliatelead_uploaded_at", "affiliatelead", ["uploaded_at"])
    op.create_index("ix_affiliatelead_next_attempt_at", "affiliatelead", ["next_attempt_at"])
    op.create_index("ix_affiliatelead_failed_at", "affiliatelead", ["failed_at"])


def downgrade() -> None:
    op.drop_table("affiliatelead")
