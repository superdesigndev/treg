"""call marks: the review a mark's rows came from

Revision ID: 0106
Revises: 0105
Create Date: 2026-11-17

Additive: one nullable column on callmark, no index, no foreign key (AGENTS.md). Set when a
review carries `rows`; existing rows stay None.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0106"
down_revision: str | Sequence[str] | None = "0105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("callmark", sa.Column("review_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("callmark", "review_id")
