"""hublisting — a hub tool's place in catalog search, requested by the maker, decided by treg

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-24

One row per tool (docs/hub-listing-decisions.md round 2): state requested | approved | rejected,
so a listing outlives a version. Expand only: `hubtool.listed` stays in the table, unread from this
revision on (the hub was off in production, so no tool was listed there), for a later contract
revision to drop. `capability` is the catalog job treg approved with the listing (round 3); `pending_version`,
`pending_pricing` and `update_reason` hold an update to an approved tool while it waits for review
(round 4); `reviewed` marks a tool treg approved once, which stays under review for good.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | Sequence[str] | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hublisting",
        sa.Column("tool_id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="requested"),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_by", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("capability", sa.String(), nullable=False, server_default=""),
        sa.Column("pending_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_pricing", sa.JSON(), nullable=True),
        sa.Column("update_reason", sa.String(), nullable=False, server_default=""),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_hublisting_org_id", "hublisting", ["org_id"])
    op.create_index("ix_hublisting_state", "hublisting", ["state"])
    op.create_index("ix_hublisting_capability", "hublisting", ["capability"])


def downgrade() -> None:
    op.drop_index("ix_hublisting_capability", table_name="hublisting")
    op.drop_index("ix_hublisting_state", table_name="hublisting")
    op.drop_index("ix_hublisting_org_id", table_name="hublisting")
    op.drop_table("hublisting")
