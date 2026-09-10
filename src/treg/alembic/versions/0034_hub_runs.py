"""hubrun — one run of a hub tool: the trace a caller and a maker read (docs/HUB-DECISIONS.md)

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-09

A new table, nothing else touched. Money stays in the ledger; this holds the trace.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hubrun",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("caller_org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("maker_org_id", sa.Integer(), nullable=False),
        sa.Column("caller_email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("cost_micro", sa.Integer(), nullable=False),
        sa.Column("price_micro", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("log", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_hubrun_run_id", "hubrun", ["run_id"])
    op.create_index("ix_hubrun_tool_id", "hubrun", ["tool_id"])
    op.create_index("ix_hubrun_caller_org_id", "hubrun", ["caller_org_id"])
    op.create_index("ix_hubrun_maker_org_id", "hubrun", ["maker_org_id"])
    op.create_index("ix_hubrun_started_at", "hubrun", ["started_at"])


def downgrade() -> None:
    for name in ("ix_hubrun_started_at", "ix_hubrun_maker_org_id", "ix_hubrun_caller_org_id",
                 "ix_hubrun_tool_id", "ix_hubrun_run_id"):
        op.drop_index(name, table_name="hubrun")
    op.drop_table("hubrun")
