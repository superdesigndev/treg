"""archive row retention cursor - where the retention job has got to in archivekey

Revision ID: 0120
Revises: 0119
Create Date: 2026-11-18

`archiveretentioncursor` holds one row: the last `archivekey.id` the row-retention job
(`treg-worker archive retain`) finished, so each bounded run resumes instead of rereading the
front of the table. `archiveobjectpending` lists object-stored bodies whose last snapshot the job
deleted, for a later run to delete from object storage after a grace period. Two new, empty
tables: no existing table is locked or rewritten.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0120"
down_revision: str | Sequence[str] | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "archiveretentioncursor",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("after_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("passes", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "archiveobjectpending",
        sa.Column("content_hash", sa.String(), primary_key=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("deleting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_archiveobjectpending_recorded_at", "archiveobjectpending", ["recorded_at"])


def downgrade() -> None:
    op.drop_table("archiveobjectpending")
    op.drop_table("archiveretentioncursor")
