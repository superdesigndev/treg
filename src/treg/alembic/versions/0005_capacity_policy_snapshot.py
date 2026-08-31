"""capacity policy + snapshot tables (docs/PROVIDER-CAPACITY-PLAN.md §2)

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-28

Alembic is the only production schema writer. SQLModel create_all remains test-fixture-only, with
the autogenerate drift guard proving it matches head.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '0005'
down_revision: str | Sequence[str] | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('capacitypolicy',
    sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('capacity_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('funding_mode', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('auto_funding_enabled', sa.Boolean(), nullable=False),
    sa.Column('auto_funding_verified_at', sa.DateTime(), nullable=True),
    sa.Column('auto_trigger_below', sa.Float(), nullable=True),
    sa.Column('auto_amount', sa.Float(), nullable=True),
    sa.Column('auto_ceiling', sa.Float(), nullable=True),
    sa.Column('target_runway_days', sa.Integer(), nullable=False),
    sa.Column('warn_days', sa.Integer(), nullable=False),
    sa.Column('urgent_days', sa.Integer(), nullable=False),
    sa.Column('critical_days', sa.Integer(), nullable=False),
    sa.Column('usd_per_unit_micro', sa.Integer(), nullable=True),
    sa.Column('owner_email', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('dashboard_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('runbook', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('overflow_allowed', sa.Boolean(), nullable=False),
    sa.Column('rate_limit', sa.JSON(), nullable=True),
    sa.Column('quota', sa.JSON(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('provider')
    )
    op.create_table('capacitysnapshot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('observed_at', sa.DateTime(), nullable=False),
    sa.Column('remaining', sa.Float(), nullable=True),
    sa.Column('total', sa.Float(), nullable=True),
    sa.Column('unit', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('resets_at', sa.DateTime(), nullable=True),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('confidence', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('note', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('error', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_capacitysnapshot_observed_at'), 'capacitysnapshot', ['observed_at'], unique=False)
    op.create_index(op.f('ix_capacitysnapshot_provider'), 'capacitysnapshot', ['provider'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_capacitysnapshot_provider'), table_name='capacitysnapshot')
    op.drop_index(op.f('ix_capacitysnapshot_observed_at'), table_name='capacitysnapshot')
    op.drop_table('capacitysnapshot')
    op.drop_table('capacitypolicy')
