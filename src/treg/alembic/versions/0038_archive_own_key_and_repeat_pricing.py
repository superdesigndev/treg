"""Own-key answers enter the archive with their origin team; repeat hits are priced per team."""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL = fetched on treg's platform key (every row before this revision).
    op.add_column("archivesnapshot", sa.Column("origin_org_id", sa.Integer(), nullable=True))
    # "org" | "conn" for a key private to an org or a connection; NULL = public (every key
    # before this revision, whose hash carries no scope).
    op.add_column("archivekey", sa.Column("scope", sa.String(), nullable=True))
    op.create_table(
        "archivekeyorg",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("first_call_at", sa.DateTime(), nullable=False),
        sa.Column("last_call_at", sa.DateTime(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.UniqueConstraint("org_id", "key_hash", name="uq_archive_key_org"),
    )
    op.create_index("ix_archivekeyorg_org_id", "archivekeyorg", ["org_id"])
    op.create_index("ix_archivekeyorg_key_hash", "archivekeyorg", ["key_hash"])


def downgrade() -> None:
    op.drop_index("ix_archivekeyorg_key_hash", table_name="archivekeyorg")
    op.drop_index("ix_archivekeyorg_org_id", table_name="archivekeyorg")
    op.drop_table("archivekeyorg")
    op.drop_column("archivekey", "scope")
    op.drop_column("archivesnapshot", "origin_org_id")
