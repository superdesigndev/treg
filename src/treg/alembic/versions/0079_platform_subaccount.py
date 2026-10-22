"""Add platformsubaccount: one upstream tenant (OpenMail pod) and its scoped key per org.

Revision ID: 0079
Revises: 0078

Additive. Older code ignores the table; nothing else changes shape.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: str | Sequence[str] | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platformsubaccount",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("upstream_id", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "provider", name="uq_platformsubaccount_org_provider"),
    )
    op.create_index("ix_platformsubaccount_org_id", "platformsubaccount", ["org_id"])
    op.create_index("ix_platformsubaccount_provider", "platformsubaccount", ["provider"])
    op.create_index("ix_platformsubaccount_upstream_id", "platformsubaccount", ["upstream_id"])


def downgrade() -> None:
    op.drop_table("platformsubaccount")
