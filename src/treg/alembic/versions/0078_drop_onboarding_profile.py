"""Drop onboardingprofile: the first-run lookup flow is gone.

Revision ID: 0078
Revises: 0077

Only the removed first-run flow read or wrote this table, and the code before this release touched
it only while one of the `TREG_ONBOARDING_V2*` settings was on, so run this once they are off
everywhere. The rows held what a lookup found about new users; nothing replaces them.

Rollback floor: older code runs on this schema only with every `TREG_ONBOARDING_V2*` setting off.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | Sequence[str] | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True


def upgrade() -> None:
    op.drop_table("onboardingprofile")


def downgrade() -> None:
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
