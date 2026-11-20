"""Add team-scoped enterprise SSO state (expand-only).

Revision ID: 0079
Revises: 0078
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: str | Sequence[str] | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enterprisesso",
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), primary_key=True),
        sa.Column("plan", sa.String(), nullable=False, server_default="sso"),
        sa.Column("subscription_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("stripe_subscription_id", sa.String(), nullable=True),
        sa.Column("stripe_event_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("workos_org_id", sa.String(), nullable=True),
        sa.Column("connection_id", sa.String(), nullable=True),
        sa.Column("directory_id", sa.String(), nullable=True),
        sa.Column("directory_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("directory_event_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("enforced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_enterprisesso_stripe_subscription_id", "enterprisesso", ["stripe_subscription_id"], unique=True)
    op.create_index("ix_enterprisesso_workos_org_id", "enterprisesso", ["workos_org_id"], unique=True)
    op.create_table(
        "enterprisessoidentity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("idp_id", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("directory_user_id", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "profile_id", name="uq_enterprise_sso_profile"),
        sa.UniqueConstraint("org_id", "user_id", name="uq_enterprise_sso_user"),
    )
    op.create_index("ix_enterprisessoidentity_org_id", "enterprisessoidentity", ["org_id"])
    op.create_index("ix_enterprisessoidentity_user_id", "enterprisessoidentity", ["user_id"])
    op.create_index("ix_enterprisessoidentity_directory_user_id", "enterprisessoidentity", ["directory_user_id"])
    op.create_table(
        "enterprisessoevent",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "enterprisedirectoryuser",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("directory_user_id", sa.String(), nullable=False),
        sa.Column("directory_id", sa.String(), nullable=False),
        sa.Column("idp_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_event_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("org_id", "directory_user_id", name="uq_enterprise_directory_user"),
    )
    op.create_index("ix_enterprisedirectoryuser_org_id", "enterprisedirectoryuser", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_enterprisedirectoryuser_org_id", table_name="enterprisedirectoryuser")
    op.drop_table("enterprisedirectoryuser")
    op.drop_table("enterprisessoevent")
    op.drop_index("ix_enterprisessoidentity_directory_user_id", table_name="enterprisessoidentity")
    op.drop_index("ix_enterprisessoidentity_user_id", table_name="enterprisessoidentity")
    op.drop_index("ix_enterprisessoidentity_org_id", table_name="enterprisessoidentity")
    op.drop_table("enterprisessoidentity")
    op.drop_index("ix_enterprisesso_workos_org_id", table_name="enterprisesso")
    op.drop_index("ix_enterprisesso_stripe_subscription_id", table_name="enterprisesso")
    op.drop_table("enterprisesso")
