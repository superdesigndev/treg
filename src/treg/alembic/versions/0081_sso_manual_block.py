"""Keep owner removal of an SSO member distinct from directory deactivation.

Revision ID: 0081
Revises: 0080
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | Sequence[str] | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("enterprisessoidentity", sa.Column(
        "manually_blocked", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("enterprisessoidentity", "manually_blocked")
