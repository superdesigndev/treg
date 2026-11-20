"""Private Web Arena runs and published, content-free live comparisons.

Revision ID: 0057
Revises: 0056
"""
from alembic import op
import sqlalchemy as sa

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("webarenarun",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False))
    for field in ("org_id", "user_id", "created_at", "expires_at"):
        op.create_index("ix_webarenarun_" + field, "webarenarun", [field])
    op.create_table("webarenapublication",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_webarenapublication_kind", "webarenapublication", ["kind"])
    op.create_table("webarenajudgebudget",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("calls", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("webarenajudgebudget")
    op.drop_table("webarenapublication")
    op.drop_table("webarenarun")
