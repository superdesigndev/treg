"""Add team-owned Lens profiles and durable quoted runs.

Revision ID: 0078
Revises: 0077
"""
from alembic import op
import sqlalchemy as sa

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("lensproject",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "request_id", name="uq_lensproject_request"))
    op.create_index("ix_lensproject_org_id", "lensproject", ["org_id"])
    op.create_index("ix_lensproject_org_updated", "lensproject", ["org_id", "updated_at"])
    op.create_table("lensrun",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(), sa.ForeignKey("lensproject.id"), nullable=True),
        sa.Column("project_revision", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.create_index("ix_lensrun_org_id", "lensrun", ["org_id"])
    op.create_index("ix_lensrun_project_id", "lensrun", ["project_id"])
    op.create_index("ix_lensrun_org_created", "lensrun", ["org_id", "created_at"])


def downgrade():
    op.drop_index("ix_lensrun_org_created", "lensrun")
    op.drop_index("ix_lensrun_project_id", "lensrun")
    op.drop_index("ix_lensrun_org_id", "lensrun")
    op.drop_table("lensrun")
    op.drop_index("ix_lensproject_org_updated", "lensproject")
    op.drop_index("ix_lensproject_org_id", "lensproject")
    op.drop_table("lensproject")
