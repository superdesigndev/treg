"""Private Arena runs and blind evaluations.

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("arenarun",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("revealed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "user_id", "request_key", name="uq_arena_request"))
    for field in ("org_id", "user_id", "expires_at"):
        op.create_index("ix_arenarun_" + field, "arenarun", [field])
    op.create_table("arenaevaluation",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("arenarun.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_arena_evaluation"))
    for field in ("org_id", "run_id"):
        op.create_index("ix_arenaevaluation_" + field, "arenaevaluation", [field])


def downgrade():
    op.drop_table("arenaevaluation")
    op.drop_table("arenarun")
