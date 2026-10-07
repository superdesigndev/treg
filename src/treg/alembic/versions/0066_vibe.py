"""vibe-it: conversations that build a hub tool, and treg's model spend per person

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-05

`vibesession` (one conversation and its draft files), `vibemessage` (its turns), `vibedraft` (every
version of the files) and `vibebudget` (what treg has spent on one person's agent; not ledger money).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | Sequence[str] | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vibesession",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("tool_id", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_test", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pending", sa.JSON(), nullable=True),
        sa.Column("running_since", sa.DateTime(), nullable=True),
        sa.Column("run_token", sa.String(), nullable=True),
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vibesession_user_id", "vibesession", ["user_id"])
    op.create_index("ix_vibesession_org_id", "vibesession", ["org_id"])
    op.create_index("ix_vibesession_updated_at", "vibesession", ["updated_at"])
    op.create_table(
        "vibemessage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("vibesession.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("cost_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vibemessage_session_id", "vibemessage", ["session_id"])
    op.create_table(
        "vibedraft",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("vibesession.id"), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=False, server_default=""),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", "n", name="uq_vibedraft_session_n"),
    )
    op.create_table(
        "vibebudget",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("spent_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vibebudget")
    op.drop_table("vibedraft")
    op.drop_index("ix_vibemessage_session_id", table_name="vibemessage")
    op.drop_table("vibemessage")
    op.drop_index("ix_vibesession_updated_at", table_name="vibesession")
    op.drop_index("ix_vibesession_org_id", table_name="vibesession")
    op.drop_index("ix_vibesession_user_id", table_name="vibesession")
    op.drop_table("vibesession")
