"""archive replay fields: the caller's query on the key, allowlisted response headers and a
history-only flag on the snapshot

Revision ID: 0091
Revises: 0090
Create Date: 2026-10-29

Three nullable columns, no default, no backfill (docs/context/architecture/archive.md, "What a
recording keeps"). Adding a nullable column without a default rewrites no rows. NULL is every
existing row: a key no caller has asked since, a snapshot whose headers were never kept, and an
answer the endpoint's `expect` rule accepts.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0091"
down_revision: str | Sequence[str] | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("archivekey", sa.Column("req_query", sa.JSON(), nullable=True))
    op.add_column("archivesnapshot", sa.Column("response_headers", sa.JSON(), nullable=True))
    op.add_column("archivesnapshot", sa.Column("history_only", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("archivesnapshot") as batch:
        batch.drop_column("history_only")
        batch.drop_column("response_headers")
    with op.batch_alter_table("archivekey") as batch:
        batch.drop_column("req_query")
