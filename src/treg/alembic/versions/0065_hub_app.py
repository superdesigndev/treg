"""hubapp: a hub tool's web page, with an optional password

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-05

One row per hub tool that has an app (`/apps/<team slug>/<name>`). The name is unique within the
team. `password_hash` is optional; `lock_version` moves on every change so an unlock remembered
under the old password stops working.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | Sequence[str] | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hubapp",
        sa.Column("tool_id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("old_names", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_hubapp_org_name"),
    )
    op.create_index("ix_hubapp_org_id", "hubapp", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_hubapp_org_id", table_name="hubapp")
    op.drop_table("hubapp")
