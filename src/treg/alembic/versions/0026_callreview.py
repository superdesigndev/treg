"""Private per-call usefulness reviews.

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "callreview",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("call_id", sa.String(), nullable=False),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("routed_via", sa.String(), nullable=True),
        sa.Column("invited", sa.Boolean(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("usefulness", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_callreview_org_id", "callreview", ['org_id'], unique=False)
    op.create_index("ix_callreview_call_id", "callreview", ['call_id'], unique=True)
    op.create_index("ix_callreview_endpoint_id", "callreview", ['endpoint_id'], unique=False)
    op.create_index("ix_callreview_endpoint_id_created_at", "callreview", ['endpoint_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_table("callreview")
