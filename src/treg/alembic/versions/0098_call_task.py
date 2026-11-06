"""calltask: the provider task an async call's answer names, for /tasks/{call_id}

Revision ID: 0098
Revises: 0097

Additive: one new table, nothing else touched. The call runtime writes a row when it relays an
async answer; `/tasks/` reads it by primary key. A code rollback leaves the table unread.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0098"
down_revision: str | Sequence[str] | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calltask",
        sa.Column("call_id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("poll_url", sa.String(), nullable=True),
        sa.Column("submission_sha256", sa.String(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_calltask_org_id", "calltask", ["org_id"])
    op.create_index("ix_calltask_created_at", "calltask", ["created_at"])


def downgrade() -> None:
    op.drop_table("calltask")
