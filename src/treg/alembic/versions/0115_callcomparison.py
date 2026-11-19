"""call comparisons: two vendors' answers to one routed call's input, the agent's verdict, and
the daily count of second-vendor calls

Revision ID: 0115
Revises: 0114
Create Date: 2026-11-19

Additive: two new tables, nothing else touched. No foreign keys (AGENTS.md): team deletion
clears a team's comparisons through ORG_SCOPED_MODELS, and every `treg-worker compare collect` run
removes any it missed. The daily count is no team's row. Written only by
`domain/feedback/comparisons.py`.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0115"
down_revision: str | Sequence[str] | None = "0114"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "callcomparison",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_email", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("call_ref", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("capability", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("a_endpoint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("a_provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("b_endpoint", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("b_provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("b_call_ref", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("b_cost_micro", sa.BigInteger(), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("a_body", sa.Text(), nullable=False),
        sa.Column("b_body", sa.Text(), nullable=False),
        sa.Column("truncated", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("first", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("choice", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("client", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_callcomparison_call_ref", "callcomparison", ["call_ref"], unique=True)
    op.create_index("ix_callcomparison_org_id_status_created_at", "callcomparison",
                    ["org_id", "status", "created_at"])
    op.create_index("ix_callcomparison_org_id_capability_created_at", "callcomparison",
                    ["org_id", "capability", "created_at"])
    op.create_index("ix_callcomparison_created_at", "callcomparison", ["created_at"])
    op.create_table(
        "compareattemptday",
        sa.Column("day", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )


def downgrade() -> None:
    op.drop_table("compareattemptday")
    op.drop_index("ix_callcomparison_created_at", table_name="callcomparison")
    op.drop_index("ix_callcomparison_org_id_capability_created_at", table_name="callcomparison")
    op.drop_index("ix_callcomparison_org_id_status_created_at", table_name="callcomparison")
    op.drop_index("ix_callcomparison_call_ref", table_name="callcomparison")
    op.drop_table("callcomparison")
