"""Opt-in referral cap exemption and durable operation history.

Revision ID: 0097
Revises: 0096

Additive defaults keep existing accounts capped. A code rollback leaves the schema in place;
downgrade removes affiliate history and exemptions and must be an explicit operator choice.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0097"
down_revision: str | Sequence[str] | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("referral_affiliate", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))
    op.add_column("user", sa.Column("referral_affiliate_version", sa.Integer(), nullable=False,
                                    server_default="0"))
    op.create_table(
        "referralaffiliateevent",
        sa.Column("operation_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("user_created_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_affiliate", sa.Boolean(), nullable=False),
        sa.Column("affiliate", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "user_created_at", "version", name="uq_referralaffiliateevent_user_version"),
        sa.CheckConstraint("version > 0", name="ck_referralaffiliateevent_version"),
        sa.CheckConstraint("length(trim(reason)) > 0 AND length(trim(principal)) > 0",
                           name="ck_referralaffiliateevent_reason_principal"),
    )
    op.create_index("ix_referralaffiliateevent_user_id", "referralaffiliateevent", ["user_id"])


def downgrade() -> None:
    op.drop_table("referralaffiliateevent")
    with op.batch_alter_table("user") as batch:
        batch.drop_column("referral_affiliate_version")
        batch.drop_column("referral_affiliate")
