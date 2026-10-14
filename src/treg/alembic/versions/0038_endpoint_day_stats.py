"""catalog: per-endpoint, per-day reliability buckets and the audit cursor that fills them

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-15

Additive: two new small tables, nothing else touched. `callrecord` is only read by the worker
that fills them, so no index on the audit table changes here.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "endpointdaystat",
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("day", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("ok", sa.Integer(), nullable=False),
        sa.Column("bad", sa.Integer(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("hit_decided", sa.Integer(), nullable=False),
        sa.Column("paid_hits", sa.Integer(), nullable=False),
        sa.Column("free_misses", sa.Integer(), nullable=False),
        sa.Column("latency_seen", sa.Integer(), nullable=False),
        sa.Column("latency_sample", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("endpoint_id", "day"),
    )
    op.create_index("ix_endpointdaystat_day", "endpointdaystat", ["day"])
    op.create_table(
        "endpointstatcursor",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cursor_id", sa.Integer(), nullable=False),
        sa.Column("watermark", sa.DateTime(), nullable=True),
        sa.Column("caught_up_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("endpointstatcursor")
    op.drop_index("ix_endpointdaystat_day", table_name="endpointdaystat")
    op.drop_table("endpointdaystat")
