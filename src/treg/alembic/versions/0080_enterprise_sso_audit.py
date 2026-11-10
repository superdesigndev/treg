"""Add durable, minimal enterprise access audit records.

Revision ID: 0080
Revises: 0079
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: str | Sequence[str] | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enterprisessoaudit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("subject_user_id", sa.Integer(), nullable=True),
        sa.Column("from_role", sa.String(), nullable=True),
        sa.Column("to_role", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_enterprisessoaudit_org_id", "enterprisessoaudit", ["org_id"])
    op.create_index("ix_enterprisessoaudit_created_at", "enterprisessoaudit", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_enterprisessoaudit_created_at", table_name="enterprisessoaudit")
    op.drop_index("ix_enterprisessoaudit_org_id", table_name="enterprisessoaudit")
    op.drop_table("enterprisessoaudit")
