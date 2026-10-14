"""Record the start of a shortened initial Web Arena observation window.

Revision ID: 0059
Revises: 0058
"""
from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("webarenacallcursor", sa.Column("observed_since", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("webarenacallcursor", "observed_since")
