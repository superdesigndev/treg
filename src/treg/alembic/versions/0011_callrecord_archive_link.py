"""callrecord.archive_key_hash / archive_content_hash — the call→archive link

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-03

Two nullable text columns, no default, no index: an instant metadata-only ALTER on Postgres even
on the hot audit table. They name the archived answer a metered platform call received, so the
team-facing `/calls/{id}/result` can show it; nothing else reads them.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("callrecord", sa.Column("archive_key_hash", sa.String(), nullable=True))
    op.add_column("callrecord", sa.Column("archive_content_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("callrecord") as batch:
        batch.drop_column("archive_content_hash")
        batch.drop_column("archive_key_hash")
