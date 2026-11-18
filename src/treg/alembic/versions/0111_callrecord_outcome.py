"""callrecord.outcome: what a v2 row's own declaration judged each answer

Revision ID: 0111
Revises: 0110
Create Date: 2026-11-18

Reliability read only the status code and `hit`, so a 2xx a v2 row declares an error counted as a
success and a target's 404 declared `not_found` counted against the provider. One nullable string,
no default and no backfill: the ALTER only takes its brief lock on a hot table, and rows without
it keep the status rule until they age out of the window.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0111"
down_revision: str | Sequence[str] | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("callrecord", sa.Column("outcome", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("callrecord") as batch:
        batch.drop_column("outcome")
