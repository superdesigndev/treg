"""Snapshot tags for pinned-token run and async read isolation.

Existing rows stay untagged: missing ownership evidence must not grant pinned access.
"""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("runrecord", "asynctaskrecord", "asyncresourcerecord", "feedback"):
        op.add_column(table, sa.Column("tags", sa.JSON(), nullable=True))


def downgrade() -> None:
    for table in ("feedback", "asyncresourcerecord", "asynctaskrecord", "runrecord"):
        op.drop_column(table, "tags")
