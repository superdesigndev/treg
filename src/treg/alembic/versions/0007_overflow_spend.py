"""overflow spend accounting (docs/PROVIDER-CAPACITY-PLAN.md §4.3 step 5)

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-28
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '0007'
down_revision: str | Sequence[str] | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('overflowspend',
    sa.Column('aggregator', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('day', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('calls', sa.Integer(), nullable=False),
    sa.Column('cost_micro', sa.Integer(), nullable=False),
    sa.Column('delta_micro', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('aggregator', 'day')
    )


def downgrade() -> None:
    op.drop_table('overflowspend')
