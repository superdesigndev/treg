"""Retain terminal hit verdict for an async submission's audit row.

Revision ID: 0052
Revises: 0051
"""

from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asynctaskrecord", sa.Column("hit", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("asynctaskrecord", "hit")
