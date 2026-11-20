"""The /rankings boards, built by the rankings worker (additive).

Revision ID: 0087
Revises: 0086
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087"
down_revision: str | Sequence[str] | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rankingssnapshot",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("through", sa.String(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("built_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rankingssnapshot")
