"""customer connections: secret.customer, pendingoauth.customer and return_to, org return origins

Revision ID: 0119
Revises: 0118
Create Date: 2026-11-20

A product built on treg lets its own customers connect their accounts. Four nullable columns, no
default and no backfill: every existing connection stays the whole team's (NULL), and an existing
team allows no return origin.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0119"
down_revision: str | Sequence[str] | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("secret", sa.Column("customer", sa.String(), nullable=True))
    op.add_column("pendingoauth", sa.Column("customer", sa.String(), nullable=True))
    op.add_column("pendingoauth", sa.Column("return_to", sa.String(), nullable=True))
    op.add_column("org", sa.Column("oauth_return_origins", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("org") as batch:
        batch.drop_column("oauth_return_origins")
    with op.batch_alter_table("pendingoauth") as batch:
        batch.drop_column("return_to")
        batch.drop_column("customer")
    with op.batch_alter_table("secret") as batch:
        batch.drop_column("customer")
