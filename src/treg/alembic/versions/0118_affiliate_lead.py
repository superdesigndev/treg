"""affiliatelead - the Dub lead owed for a team an affiliate brought (link click or partner code)

Revision ID: 0118
Revises: 0117
Create Date: 2026-11-19

A new outbox table, one row per attributed team. Additive: no existing row changes. No foreign
key: team deletion removes the row in code (`ORG_SCOPED_MODELS`).
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
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("click_id", sa.String(), nullable=True),
        sa.Column("partner_code", sa.String(), nullable=True),
        sa.Column("country", sa.String(), nullable=False, server_default=""),
        sa.Column("partner_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("stripe_synced_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_affiliatelead_org"),
        sa.CheckConstraint("(click_id IS NULL) <> (partner_code IS NULL)",
                           name="ck_affiliatelead_one_source"),
    )
    op.create_index("ix_affiliatelead_user_id", "affiliatelead", ["user_id"])
    op.create_index("ix_affiliatelead_uploaded_at", "affiliatelead", ["uploaded_at"])
    op.create_index("ix_affiliatelead_verified_at", "affiliatelead", ["verified_at"])
    op.create_index("ix_affiliatelead_stripe_synced_at", "affiliatelead", ["stripe_synced_at"])
    op.create_index("ix_affiliatelead_next_attempt_at", "affiliatelead", ["next_attempt_at"])
    op.create_index("ix_affiliatelead_failed_at", "affiliatelead", ["failed_at"])
    op.create_table(
        "affiliateclaim",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("inbox", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_affiliateclaim_inbox", "affiliateclaim", ["inbox"])


def downgrade() -> None:
    op.drop_table("affiliateclaim")
    op.drop_table("affiliatelead")
