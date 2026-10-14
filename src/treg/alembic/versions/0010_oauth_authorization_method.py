"""separate OAuth authorization methods on pending and stored grants

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-01

Existing Instagram grants were created only through Facebook Login, so the backfill can identify
them without inspecting token material. Empty remains the compatibility value for every provider
whose one method predates this column.

Rollback floor: downgrading removes the authorization-method identity after new direct Instagram
grants may have been written, so an older build could mistake them for legacy Facebook Page grants.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True


def upgrade() -> None:
    op.add_column(
        "pendingoauth",
        sa.Column("authorization_method", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "pendingoauth",
        sa.Column("long_lived_exchange_style", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "secret",
        sa.Column("authorization_method", sa.String(), nullable=False, server_default=""),
    )
    op.create_index(
        op.f("ix_secret_authorization_method"), "secret", ["authorization_method"], unique=False
    )
    op.execute(
        sa.text(
            "UPDATE secret SET authorization_method = 'facebook-page' "
            "WHERE provider = 'instagram' AND authorization_method = ''"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("secret") as batch:
        batch.drop_index(op.f("ix_secret_authorization_method"))
        batch.drop_column("authorization_method")
    with op.batch_alter_table("pendingoauth") as batch:
        batch.drop_column("long_lived_exchange_style")
        batch.drop_column("authorization_method")
