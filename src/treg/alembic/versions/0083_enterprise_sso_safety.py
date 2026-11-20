"""Track SSO-created memberships and serialize enterprise checkout.

Revision ID: 0083
Revises: 0082
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | Sequence[str] | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("apikey", sa.Column("sso_org_id", sa.Integer(), nullable=True))
    op.add_column("enterprisesso", sa.Column("checkout_token", sa.String(), nullable=True))
    op.add_column("enterprisesso", sa.Column("checkout_pending_until", sa.Integer(),
                                             nullable=False, server_default="0"))
    op.add_column("enterprisesso", sa.Column("checkout_url", sa.String(), nullable=True))
    op.add_column("enterprisessoidentity", sa.Column("membership_created_by_sso", sa.Boolean(),
                                                     nullable=False, server_default=sa.false()))
    with op.batch_alter_table("enterprisessoidentity") as batch_op:
        batch_op.create_unique_constraint("uq_enterprise_sso_idp", ["org_id", "idp_id"])


def downgrade() -> None:
    with op.batch_alter_table("enterprisessoidentity") as batch_op:
        batch_op.drop_constraint("uq_enterprise_sso_idp", type_="unique")
    op.drop_column("enterprisessoidentity", "membership_created_by_sso")
    op.drop_column("enterprisesso", "checkout_url")
    op.drop_column("enterprisesso", "checkout_pending_until")
    op.drop_column("enterprisesso", "checkout_token")
    op.drop_column("apikey", "sso_org_id")
