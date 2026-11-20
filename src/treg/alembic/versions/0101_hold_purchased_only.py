"""hold.purchased_only - a hold that only purchased credit may settle

Revision ID: 0101
Revises: 0100
Create Date: 2026-11-09

An endpoint on a platform declaring `purchased_credit_only` reserves against purchased blocks and
must settle from them alone, on every settle path (the call, the async worker, a batch close). The
flag lives on the hold so each of those paths reads it from the row it already claims. Additive,
with a false default: every existing hold keeps today's consumption order.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0101"
down_revision: str | Sequence[str] | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hold", sa.Column("purchased_only", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("hold") as batch:
        batch.drop_column("purchased_only")
