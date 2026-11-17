"""org.topup_bonus_pct - a team's own top-up bonus rate

Revision ID: 0111
Revises: 0110
Create Date: 2026-11-18

Additive: one nullable column, null for every existing team, which keeps the global tiers.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0111"
down_revision: str | Sequence[str] | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org", sa.Column("topup_bonus_pct", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("org", "topup_bonus_pct")
