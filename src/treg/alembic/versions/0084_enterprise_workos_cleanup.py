"""Track completed WorkOS teardown after an SSO subscription ends.

Revision ID: 0084
Revises: 0083
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084"
down_revision: str | Sequence[str] | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("enterprisesso", sa.Column("workos_cleanup_completed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("enterprisesso", "workos_cleanup_completed_at")
