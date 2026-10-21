"""Retain authenticated provider subjects and verified inbox links for signup credit.

Revision ID: 0079
Revises: 0078

This is a rollback floor: dropping evidence reopens claimed provider identities. Historical
provider subjects were not stored and cannot be reconstructed. New observations consult the
existing permanent inbox claims. All grant writers must be upgraded before enabling the guard.
"""
from alembic import op
import sqlalchemy as sa

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None
contract = True


def upgrade() -> None:
    op.create_table(
        "signuppromoidentity",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("claimed", sa.Boolean(), nullable=False),
        sa.Column("account_created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "signuppromolink",
        sa.Column("inbox_key", sa.String(), primary_key=True),
        sa.Column("subject_key", sa.String(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("signuppromolink")
    op.drop_table("signuppromoidentity")
