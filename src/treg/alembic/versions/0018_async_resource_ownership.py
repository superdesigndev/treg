"""Persist org ownership of shared-provider async task and result ids.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-04
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("asynctaskrecord", sa.Column("result_id", sa.String(), nullable=True))
    op.create_index(op.f("ix_asynctaskrecord_result_id"), "asynctaskrecord", ["result_id"])
    op.create_table(
        "asyncresourcerecord",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("resource_kind", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("source_call_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "provider", "resource_kind", "resource_id",
            name="uq_asyncresource_org_provider_kind_id",
        ),
    )
    for column in ("org_id", "provider", "resource_kind", "resource_id", "source_call_id", "created_at"):
        op.create_index(op.f(f"ix_asyncresourcerecord_{column}"), "asyncresourcerecord", [column])


def downgrade() -> None:
    op.drop_table("asyncresourcerecord")
    op.drop_index(op.f("ix_asynctaskrecord_result_id"), table_name="asynctaskrecord")
    op.drop_column("asynctaskrecord", "result_id")
