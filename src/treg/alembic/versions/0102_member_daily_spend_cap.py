"""member daily spend cap - a dollar limit per member, a team default, and the rows it reads

Revision ID: 0102
Revises: 0101
Create Date: 2026-11-12

`membership.daily_spend_cap_micro` is one member's own limit (NULL follows the team, -1 is none),
`org.member_daily_spend_cap_micro` the team default (NULL = none). `memberspend` holds one row per
metered call of a member whose limit applies, which is what the limit is checked against.
Additive: every existing member and team stays unlimited.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0102"
down_revision: str | Sequence[str] | None = "0101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("membership", sa.Column("daily_spend_cap_micro", sa.BigInteger(), nullable=True))
    op.add_column("org", sa.Column("member_daily_spend_cap_micro", sa.BigInteger(), nullable=True))
    op.create_table(
        "memberspend",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("membership_id", sa.Integer(), nullable=False),
        sa.Column("hold_id", sa.String(), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_memberspend_org_created", "memberspend", ["org_id", "created_at"])
    op.create_index("ix_memberspend_hold_id", "memberspend", ["hold_id"])
    op.create_index("ix_memberspend_membership_created", "memberspend",
                    ["membership_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_memberspend_membership_created", table_name="memberspend")
    op.drop_index("ix_memberspend_hold_id", table_name="memberspend")
    op.drop_index("ix_memberspend_org_created", table_name="memberspend")
    op.drop_table("memberspend")
    with op.batch_alter_table("org") as batch:
        batch.drop_column("member_daily_spend_cap_micro")
    with op.batch_alter_table("membership") as batch:
        batch.drop_column("daily_spend_cap_micro")
