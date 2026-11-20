"""org.autotopup_max_per_hour: automatic charges allowed per hour after a successful one

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-05

One charge an hour left a team that spends more than its refill per hour at $0 for the rest of the
hour. The team's own number (0 = the deployment default, 5) is stored next to the amounts it agreed
to. NOT NULL with a server default of 0, so existing rows need no backfill.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | Sequence[str] | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org", sa.Column("autotopup_max_per_hour", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("org") as batch:
        batch.drop_column("autotopup_max_per_hour")
