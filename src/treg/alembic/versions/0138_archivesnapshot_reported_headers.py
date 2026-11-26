"""archivesnapshot.reported_headers: the reported-charge header a hit settles from

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-26

A v2 row whose `settle.reported` reads a response header settles a live answer from it, but an
archive hit is served from stored bytes, which carry no provider header. The snapshot now keeps
that one header's raw value beside the answer ({name: value}, {} when the provider sent none), so
a hit settles as the live answer did. One nullable JSON column, no default and no backfill: the
ALTER only takes its brief lock on a large table, and a NULL row (every row before this revision)
settles a hit by the outcome rules and the hold, as before.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0138"
down_revision: str | Sequence[str] | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("archivesnapshot", sa.Column("reported_headers", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("archivesnapshot") as batch:
        batch.drop_column("reported_headers")
