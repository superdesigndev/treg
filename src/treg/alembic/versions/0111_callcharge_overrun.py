"""callcharge.overrun_micro - the part of a charge its call never set aside

Revision ID: 0111
Revises: 0110
Create Date: 2026-11-18

A call that cost more than it reserved from its team's allowance owes the difference to the
balance, not to the allowance hold: the hold still stands behind other calls in flight. The column
keeps that part apart so applying the charge and checking a daily cap can both see it. Additive,
default 0; the table is a short-lived queue and nothing has written to it yet.
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


def downgrade() -> None:
    with op.batch_alter_table("callcharge") as batch:
        batch.drop_column("overrun_micro")
