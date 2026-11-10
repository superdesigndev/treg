"""Incremental, content-free Web Arena observations from direct provider calls.

Revision ID: 0058
Revises: 0057
"""
from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("webarenacalldaystat",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("day", sa.String(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("decided", sa.Integer(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("timed", sa.Integer(), nullable=False),
        sa.Column("duration_sum_ms", sa.Integer(), nullable=False),
        sa.Column("duration_sample", sa.JSON(), nullable=False),
        sa.UniqueConstraint("endpoint_id", "day", name="uq_webarenacalldaystat_endpoint_day"))
    op.create_index("ix_webarenacalldaystat_endpoint_id", "webarenacalldaystat", ["endpoint_id"])
    op.create_index("ix_webarenacalldaystat_day", "webarenacalldaystat", ["day"])
    op.create_table("webarenacallcursor",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table("webarenacallcursor")
    op.drop_index("ix_webarenacalldaystat_day", "webarenacalldaystat")
    op.drop_index("ix_webarenacalldaystat_endpoint_id", "webarenacalldaystat")
    op.drop_table("webarenacalldaystat")
