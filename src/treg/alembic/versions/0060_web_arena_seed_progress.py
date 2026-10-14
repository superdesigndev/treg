"""Persist bounded Web Arena seed progress and aggregate staging buckets.

Revision ID: 0060
Revises: 0059
"""
from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("webarenaseedprogress",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("observed_since", sa.DateTime(), nullable=False),
        sa.Column("lagged_until", sa.DateTime(), nullable=False),
        sa.Column("first_id", sa.Integer(), nullable=False),
        sa.Column("highwater_id", sa.Integer(), nullable=False),
        sa.Column("endpoints", sa.JSON(), nullable=False),
        sa.Column("last_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_index", sa.Integer(), nullable=False),
        sa.Column("scanned", sa.Integer(), nullable=False),
        sa.Column("eligible_calls", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_table("webarenaseeddaystat",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("day", sa.String(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("decided", sa.Integer(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("timed", sa.Integer(), nullable=False),
        sa.Column("duration_sum_ms", sa.Integer(), nullable=False),
        sa.Column("duration_sample", sa.JSON(), nullable=False),
        sa.UniqueConstraint("endpoint_id", "day", name="uq_webarenaseeddaystat_endpoint_day"))
    op.create_index("ix_webarenaseeddaystat_endpoint_id", "webarenaseeddaystat", ["endpoint_id"])
    op.create_index("ix_webarenaseeddaystat_day", "webarenaseeddaystat", ["day"])


def downgrade():
    op.drop_index("ix_webarenaseeddaystat_day", "webarenaseeddaystat")
    op.drop_index("ix_webarenaseeddaystat_endpoint_id", "webarenaseeddaystat")
    op.drop_table("webarenaseeddaystat")
    op.drop_table("webarenaseedprogress")
