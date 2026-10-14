"""onboardingprofile: one new user's first-run lookup

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-01

What the onboarding found about a new user (encrypted payload), the first tasks it ranked for them
with their filled-in inputs, and what the lookup cost treg's house team. One row per user.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | Sequence[str] | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboardingprofile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("house_cost_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_onboardingprofile_user_id", "onboardingprofile", ["user_id"], unique=True)
    op.create_index("ix_onboardingprofile_org_id", "onboardingprofile", ["org_id"])


def downgrade() -> None:
    op.drop_table("onboardingprofile")
