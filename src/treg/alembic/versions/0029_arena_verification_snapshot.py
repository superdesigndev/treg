"""Persist published verification aggregates separately from rolling observations.

Revision ID: 0029
Revises: 0028
"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("arenaverificationsnapshot",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_digest", sa.String(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True))
    op.create_index("ix_arenaverificationsnapshot_published_at", "arenaverificationsnapshot", ["published_at"])


def downgrade():
    op.drop_index("ix_arenaverificationsnapshot_published_at", table_name="arenaverificationsnapshot")
    op.drop_table("arenaverificationsnapshot")
