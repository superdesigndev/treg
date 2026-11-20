"""routeruleversion: saved, reviewed and published versions of the route rule set

Revision ID: 0096
Revises: 0095
Create Date: 2026-11-05

Additive: one new table, nothing else touched. The private admin is its only writer; the call path
never reads it (workers read the published snapshot from Redis,
docs/context/architecture/route-rules.md).
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0096"
down_revision: str | Sequence[str] | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "routeruleversion",
        sa.Column("version", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("config", sa.JSON(none_as_null=True).with_variant(
            JSONB(none_as_null=True), "postgresql"), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("review_status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.String(), nullable=True),
        sa.Column("publish_status", sa.String(), nullable=False, server_default="unpublished"),
        sa.Column("published_by", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_routeruleversion_version"),
        sa.CheckConstraint("review_status IN ('draft', 'approved', 'rejected')",
                           name="ck_routeruleversion_review_status"),
        sa.CheckConstraint("publish_status IN ('unpublished', 'pending', 'published', 'failed')",
                           name="ck_routeruleversion_publish_status"),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0 AND length(trim(reason)) > 0",
            name="ck_routeruleversion_created"),
        sa.CheckConstraint(
            "review_status = 'draft' OR (reviewed_by IS NOT NULL AND length(trim(reviewed_by)) > 0"
            " AND reviewed_at IS NOT NULL)",
            name="ck_routeruleversion_reviewed"),
        sa.CheckConstraint(
            "publish_status = 'unpublished' OR review_status = 'approved'",
            name="ck_routeruleversion_publish_approved"),
        sa.CheckConstraint(
            "publish_status != 'published' OR "
            "(published_by IS NOT NULL AND length(trim(published_by)) > 0 AND published_at IS NOT NULL)",
            name="ck_routeruleversion_published"),
    )
    op.create_index("ix_routeruleversion_publish_status", "routeruleversion",
                    ["publish_status", "version"])


def downgrade() -> None:
    # Schema downgrade removes saved history; routine application rollback leaves this table.
    op.drop_table("routeruleversion")
