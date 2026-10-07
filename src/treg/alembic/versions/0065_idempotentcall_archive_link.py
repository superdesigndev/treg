"""idempotentcall.archive_key_hash / archive_content_hash: a retry answer kept in the archive

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-05

A retry row whose answer the archive already holds drops its own copy and names the archive entry
instead. Two nullable columns, no default and no backfill, so the ALTER only takes its brief lock.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | Sequence[str] | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("idempotentcall", sa.Column("archive_key_hash", sa.String(), nullable=True))
    op.add_column("idempotentcall", sa.Column("archive_content_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("idempotentcall") as batch:
        batch.drop_column("archive_content_hash")
        batch.drop_column("archive_key_hash")
