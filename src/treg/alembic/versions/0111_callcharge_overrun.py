"""callcharge.overrun_micro and hold_created_at - what a charge may take from which hold

Revision ID: 0111
Revises: 0110
Create Date: 2026-11-18

A call that cost more than it reserved from its team's allowance owes the difference to the
balance, not to the allowance hold: the hold still stands behind other calls in flight.
`overrun_micro` keeps that part apart. `hold_created_at` names the hold the call set its money
aside from, so a charge written late is not paid out of a hold opened since. Both additive; the
table is a short-lived queue and nothing has written to it yet.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0111"
down_revision: str | Sequence[str] | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("callcharge", sa.Column("overrun_micro", sa.BigInteger(), nullable=False,
                                          server_default="0"))
    op.add_column("callcharge", sa.Column("hold_created_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("callcharge") as batch:
        batch.drop_column("hold_created_at")
        batch.drop_column("overrun_micro")
