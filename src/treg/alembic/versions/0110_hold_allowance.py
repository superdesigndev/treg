"""hold.allowance and hold.heartbeat_at - a hold that is a team's standing allowance

Revision ID: 0110
Revises: 0109
Create Date: 2026-11-17

A hold can stand for a team's allowance instead of one call's reservation: one row per team,
topped up in place, alive for as long as its holder keeps renewing it. The flag tells such a row
from a call's hold, and the heartbeat says whether its holder is still there. Additive, with a
false default and a null heartbeat: every existing hold stays a call's hold, and nothing sets
either column yet.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0110"
down_revision: str | Sequence[str] | None = "0109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hold", sa.Column("allowance", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))
    op.add_column("hold", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("hold") as batch:
        batch.drop_column("heartbeat_at")
        batch.drop_column("allowance")
