"""endpointdaystat.verdicts: calls per contract verdict word, per endpoint per day

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-07

The folded read model counts `callrecord.verdict` the way it counts `hit`. One nullable JSON column
on a small table the stats worker alone writes; a bucket folded before it existed reads as empty,
which is true, since no call before `0065` carried a verdict.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | Sequence[str] | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("endpointdaystat", sa.Column("verdicts", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("endpointdaystat") as batch:
        batch.drop_column("verdicts")
