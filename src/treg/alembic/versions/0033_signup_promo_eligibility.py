"""Verified identity and once-per-user signup credit; old users are ineligible."""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    # Retain the fail-closed default for old writers during a rolling deployment. New
    # application User instances explicitly insert True; no historical ledger scan needed.
    op.add_column("user", sa.Column("signup_promo_available", sa.Boolean(),
                                   nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("user", "signup_promo_available")
    op.drop_column("user", "email_verified_at")
