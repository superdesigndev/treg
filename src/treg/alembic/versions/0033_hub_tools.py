"""hubtool — one version of a tool a maker published on the hub (docs/HUB-DECISIONS.md)

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-09

A new table, nothing else touched. Off behind TREG_HUB_ENABLED until the whole hub lands.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hubtool",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("writes", sa.Boolean(), nullable=False),
        sa.Column("price_micro", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("script", sa.String(), nullable=True),
        sa.Column("check", sa.JSON(), nullable=False),
        sa.Column("readme", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tool_id", "version", name="uq_hubtool_id_version"),
    )
    op.create_index("ix_hubtool_org_id", "hubtool", ["org_id"])
    op.create_index("ix_hubtool_tool_id", "hubtool", ["tool_id"])


def downgrade() -> None:
    op.drop_index("ix_hubtool_tool_id", table_name="hubtool")
    op.drop_index("ix_hubtool_org_id", table_name="hubtool")
    op.drop_table("hubtool")
