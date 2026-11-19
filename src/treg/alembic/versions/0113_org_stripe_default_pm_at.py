"""org.stripe_default_pm_at - when the payer chose the card auto top-up charges

Revision ID: 0113
Revises: 0112
Create Date: 2026-11-19

The card can be chosen in treg or in the Stripe Customer Portal. The Stripe event time of the
latest choice orders the two, so a late or redelivered event never brings back an older card.
Additive and nullable: existing rows keep their card with no recorded time.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0113"
down_revision: str | Sequence[str] | None = "0112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org", sa.Column("stripe_default_pm_at", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("org") as batch:
        batch.drop_column("stripe_default_pm_at")
