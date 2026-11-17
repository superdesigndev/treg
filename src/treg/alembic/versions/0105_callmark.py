"""call marks: what an agent found wrong or confirmed right in one call's answer

Revision ID: 0105
Revises: 0104
Create Date: 2026-11-16

Additive: one new table, nothing else touched. No foreign keys (AGENTS.md); team deletion
clears its rows through ORG_SCOPED_MODELS. Written only by `domain/feedback/marks.py`.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0105"
down_revision: str | Sequence[str] | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "callmark",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_email", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("call_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("credential_tier", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("client", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("basis", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("checked", sa.Integer(), nullable=True),
        sa.Column("evidence_call_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("evidence_verified", sa.Boolean(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("replaces", sa.Integer(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("content_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_callmark_org_id", "callmark", ["org_id"])
    op.create_index("ix_callmark_call_id", "callmark", ["call_id"])
    op.create_index("ix_callmark_endpoint_id_created_at", "callmark", ["endpoint_id", "created_at"])
    op.create_index("ix_callmark_org_id_content_hash", "callmark", ["org_id", "content_hash"],
                    unique=True)


def downgrade() -> None:
    op.drop_index("ix_callmark_org_id_content_hash", table_name="callmark")
    op.drop_index("ix_callmark_endpoint_id_created_at", table_name="callmark")
    op.drop_index("ix_callmark_call_id", table_name="callmark")
    op.drop_index("ix_callmark_org_id", table_name="callmark")
    op.drop_table("callmark")
