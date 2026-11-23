"""Jev's judgements of a review's reason: whether it names a person, and what it blames.

Revision ID: 0124
Revises: 0123
"""
from alembic import op
import sqlalchemy as sa

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

COLUMNS = (
    sa.Column("person_p", sa.Float(), nullable=True),
    sa.Column("privacy_judged_at", sa.DateTime(), nullable=True),
    sa.Column("primary_code", sa.String(), nullable=True),
    sa.Column("secondary_code", sa.String(), nullable=True),
    sa.Column("codebook_version", sa.String(), nullable=True),
    sa.Column("code_judged_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    # Nullable columns without defaults: a catalog-only change, no table rewrite.
    for column in COLUMNS:
        op.add_column("callreview", column)


def downgrade() -> None:
    for column in reversed(COLUMNS):
        op.drop_column("callreview", column.name)
