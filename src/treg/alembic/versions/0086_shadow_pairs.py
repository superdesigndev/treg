"""Shadow provider pairs and their cursor (additive).

Revision ID: 0086
Revises: 0085
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086"
down_revision: str | Sequence[str] | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadowcursor",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("cursor_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "shadowpair",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("served_found", sa.Boolean(), nullable=False),
        sa.Column("shadow_found", sa.Boolean(), nullable=False),
        sa.Column("served_count", sa.Integer(), nullable=True),
        sa.Column("shadow_count", sa.Integer(), nullable=True),
        sa.Column("served_with_email", sa.Integer(), nullable=True),
        sa.Column("shadow_with_email", sa.Integer(), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("shadow_status", sa.Integer(), nullable=False),
        sa.Column("shadow_ms", sa.Integer(), nullable=True),
        sa.Column("shadow_units", sa.Float(), nullable=True),
        sa.Column("served_cost_micro", sa.Integer(), nullable=False),
    )
    op.create_index("ix_shadowpair_created_at", "shadowpair", ["created_at"])
    op.create_index("ix_shadowpair_capability", "shadowpair", ["capability"])


def downgrade() -> None:
    op.drop_index("ix_shadowpair_capability", "shadowpair")
    op.drop_index("ix_shadowpair_created_at", "shadowpair")
    op.drop_table("shadowpair")
    op.drop_table("shadowcursor")
