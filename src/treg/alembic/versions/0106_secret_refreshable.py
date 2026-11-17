"""secret.refreshable: whether treg can renew an OAuth credential unattended

Revision ID: 0106
Revises: 0105
Create Date: 2026-11-17

Additive: one nullable column, no backfill here. Listing connections reads it instead of opening
every credential; a null on a stored Fernet value falls back to opening it locally, and the
credential re-encryption job fills it.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0106"
down_revision: str | Sequence[str] | None = "0105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("secret", sa.Column("refreshable", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("secret", "refreshable")
