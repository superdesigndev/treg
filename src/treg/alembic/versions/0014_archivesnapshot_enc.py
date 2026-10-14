"""archivesnapshot.enc — how the stored body is encoded on disk

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03

NULL = raw bytes (every row before this revision), "zlib" = compressed. New writes compress when
it shrinks (measured 5.2x on real bodies); old rows stay raw and readable. size_bytes and
content_hash always describe the RAW bytes, so statistics and dedup semantics do not move.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("archivesnapshot", sa.Column("enc", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("archivesnapshot") as batch:
        batch.drop_column("enc")
