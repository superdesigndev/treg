"""Preserve SSO proof on consent codes and refresh families.

Revision ID: 0082
Revises: 0081
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | Sequence[str] | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("oauthcode", sa.Column("sso_org_id", sa.Integer(), nullable=True))
    op.add_column("oauthgrant", sa.Column("sso_org_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("oauthgrant", "sso_org_id")
    op.drop_column("oauthcode", "sso_org_id")
