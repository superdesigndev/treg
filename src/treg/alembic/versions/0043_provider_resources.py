"""Durable organization ownership for resources created with platform credentials."""

from alembic import op
import sqlalchemy as sa


revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "providerresource",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("resource_kind", sa.String(), nullable=False),
        sa.Column("upstream_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("source_call_id", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "resource_kind", "upstream_id",
            name="uq_providerresource_provider_kind_upstream",
        ),
    )
    op.create_index("ix_providerresource_org_id", "providerresource", ["org_id"])
    op.create_index("ix_providerresource_provider", "providerresource", ["provider"])
    op.create_index("ix_providerresource_resource_kind", "providerresource", ["resource_kind"])
    op.create_index("ix_providerresource_upstream_id", "providerresource", ["upstream_id"])
    op.create_index("ix_providerresource_source_call_id", "providerresource", ["source_call_id"])
    op.create_index("ix_providerresource_status", "providerresource", ["status"])
    op.create_index("ix_providerresource_created_at", "providerresource", ["created_at"])
    op.create_index("ix_providerresource_deleted_at", "providerresource", ["deleted_at"])
    op.create_index(
        "ix_providerresource_org_provider_kind_status", "providerresource",
        ["org_id", "provider", "resource_kind", "status"],
    )


def downgrade() -> None:
    op.drop_table("providerresource")
