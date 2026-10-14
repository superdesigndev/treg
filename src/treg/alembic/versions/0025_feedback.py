"""Durable, team-scoped feedback intake.

Revision ID: 0025
Revises: 0024
"""

from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("call_ids", sa.JSON(), nullable=False),
        sa.Column("verified_call_ids", sa.JSON(), nullable=False),
        sa.Column("endpoint_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_feedback_org_id", "feedback", ["org_id"])
    op.create_index("ix_feedback_category", "feedback", ["category"])


def downgrade() -> None:
    op.drop_table("feedback")
