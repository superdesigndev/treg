"""media: hosted reference files for vendors to fetch (`treg host`)

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-14

Additive: one new table, nothing else touched.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("content_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_token", "media", ["token"], unique=True)
    op.create_index("ix_media_expires_at", "media", ["expires_at"])
    op.create_index("ix_media_org_created", "media", ["org_id", "created_at"])


def downgrade() -> None:
    op.drop_table("media")
