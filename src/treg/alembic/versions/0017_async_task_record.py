"""Async task records for deferred settlement.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-02

This is an expand-only table addition. It ships with the request/worker behavior because old code
ignores the table and new code cannot defer a hold safely until the durable row exists.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asynctaskrecord",
        sa.Column("call_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("task_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("poll_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reserved_micro", sa.Integer(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column("settlement_basis", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("settled_micro", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("call_id"),
    )
    op.create_index(op.f("ix_asynctaskrecord_org_id"), "asynctaskrecord", ["org_id"])
    op.create_index(op.f("ix_asynctaskrecord_provider"), "asynctaskrecord", ["provider"])
    op.create_index(op.f("ix_asynctaskrecord_endpoint_id"), "asynctaskrecord", ["endpoint_id"])
    op.create_index(op.f("ix_asynctaskrecord_task_id"), "asynctaskrecord", ["task_id"])
    op.create_index(op.f("ix_asynctaskrecord_created_at"), "asynctaskrecord", ["created_at"])
    op.create_index(op.f("ix_asynctaskrecord_next_check_at"), "asynctaskrecord", ["next_check_at"])
    op.create_index(op.f("ix_asynctaskrecord_status"), "asynctaskrecord", ["status"])
    op.create_index(op.f("ix_asynctaskrecord_completed_at"), "asynctaskrecord", ["completed_at"])


def downgrade() -> None:
    op.drop_table("asynctaskrecord")
