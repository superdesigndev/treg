"""overflow route table (docs/PROVIDER-CAPACITY-PLAN.md §4.3)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-28
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '0006'
down_revision: str | Sequence[str] | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('overflowroute',
    sa.Column('endpoint_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('aggregator', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('method', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('path', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('agg_slug', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('agg_path', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('agg_price_micro', sa.Integer(), nullable=True),
    sa.Column('agg_unit', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('ratio', sa.Float(), nullable=True),
    sa.Column('single_result', sa.Boolean(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('disabled_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('matched_at', sa.DateTime(), nullable=True),
    sa.Column('last_verified_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('endpoint_id', 'aggregator')
    )
    op.create_index(op.f('ix_overflowroute_enabled'), 'overflowroute', ['enabled'], unique=False)
    op.create_index(op.f('ix_overflowroute_provider'), 'overflowroute', ['provider'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_overflowroute_provider'), table_name='overflowroute')
    op.drop_index(op.f('ix_overflowroute_enabled'), table_name='overflowroute')
    op.drop_table('overflowroute')
