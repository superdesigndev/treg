"""A renamed team keeps its old slug as an alias so pinned credentials still resolve."""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org", sa.Column("previous_slug", sa.String(), nullable=True))
    op.create_index("ix_org_previous_slug", "org", ["previous_slug"])


def downgrade() -> None:
    op.drop_index("ix_org_previous_slug", table_name="org")
    op.drop_column("org", "previous_slug")
