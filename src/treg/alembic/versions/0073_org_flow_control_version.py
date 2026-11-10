"""Store versioned Org flow-control configuration without enabling request enforcement.

Revision ID: 0073
Revises: 0072

Additive only: no backfill, role grants, or changes to existing Org rows.
No foreign key to org: creating configuration storage must not lock the balance table.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orgflowcontrolversion",
        sa.Column("org_id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("config", sa.JSON(none_as_null=True).with_variant(
            JSONB(none_as_null=True), "postgresql"), nullable=False),
        sa.Column("publish_status", sa.String(), nullable=False, server_default="unpublished"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("published_by", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_orgflowcontrolversion_version"),
        sa.CheckConstraint(
            "publish_status IN ('unpublished', 'pending', 'published', 'failed')",
            name="ck_orgflowcontrolversion_publish_status",
        ),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0 AND "
            "(published_by IS NULL OR length(trim(published_by)) > 0)",
            name="ck_orgflowcontrolversion_actors",
        ),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_orgflowcontrolversion_reason"),
        sa.CheckConstraint(
            "publish_status != 'published' OR "
            "(published_by IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_orgflowcontrolversion_published",
        ),
    )
    op.create_index(
        "ix_orgflowcontrolversion_publish_status", "orgflowcontrolversion",
        ["publish_status", "org_id", "version"],
    )


def downgrade() -> None:
    # Schema downgrade removes saved history; routine application rollback leaves this table.
    op.drop_table("orgflowcontrolversion")
