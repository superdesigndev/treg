"""team-specific generation for rotatable signed Default keys

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-04
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "apikey",
        sa.Column("default_generation", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("apikey", "default_generation")
