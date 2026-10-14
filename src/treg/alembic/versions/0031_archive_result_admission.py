"""Separate decisive cache evidence from the latest historical response."""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("archivekey", sa.Column("result_state", sa.String(), nullable=True))
    op.add_column("archivekey", sa.Column("result_snapshot_id", sa.Integer(), nullable=True))
    op.add_column("archivekey", sa.Column("result_observed_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("archivekey", "result_observed_version")
    op.drop_column("archivekey", "result_snapshot_id")
    op.drop_column("archivekey", "result_state")
