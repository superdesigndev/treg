"""Database-backed Arena observations and aggregate cursor.

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("arenaobservation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("input", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_arenaobservation_window", "arenaobservation", ["version", "created_at"])
    op.create_index("ix_arenaobservation_request", "arenaobservation", ["version", "endpoint", "input", "request_hash", "created_at"])
    op.create_table("arenainsightstate",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("scan_until", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("arenainsightstate")
    op.drop_table("arenaobservation")
