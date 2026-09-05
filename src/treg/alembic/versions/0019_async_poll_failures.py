"""Persist consecutive poll failures for bounded provider-error backoff.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asynctaskrecord", sa.Column(
        "consecutive_failures", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("asynctaskrecord", "consecutive_failures")
