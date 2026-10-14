"""callrecord.verdict and asynctaskrecord.verdict: the contract's verdict word for each answer

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-07

`hit` says only whether a provider found something. A verifier's `catch_all` or `unknown` and a
finder's own `verified` claim were read and dropped. One nullable string per table, no default and
no backfill: the ALTER only takes its brief lock on a hot table. The task row keeps the async
terminal word for the same insert race `hit` has.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | Sequence[str] | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("callrecord", sa.Column("verdict", sa.String(), nullable=True))
    op.add_column("asynctaskrecord", sa.Column("verdict", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("asynctaskrecord") as batch:
        batch.drop_column("verdict")
    with op.batch_alter_table("callrecord") as batch:
        batch.drop_column("verdict")
