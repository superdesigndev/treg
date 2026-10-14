"""callrecord.cached — the archive's serve tag (PR 4; treg/archive.py serving)

One boolean on the audit row: True when the archive answered instead of the vendor. Money
columns stay identical to a live call on purpose — pricing a hit is a deferred founder decision
that will attach to this tag. server_default false backfills every historical row correctly:
nothing before this migration was ever served from the store.

Rollback floor: the new NOT NULL column keeps no server default once backfilled, so code older
than this revision can no longer insert call records.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-27

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: str | Sequence[str] | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('callrecord', sa.Column('cached', sa.Boolean(), nullable=False,
                                          server_default=sa.false()))
    # Drop the backfill default so the schema matches the model (no server default). SQLite cannot
    # ALTER a default in place; batch mode rebuilds the table there and is a plain ALTER on Postgres.
    with op.batch_alter_table('callrecord') as batch:
        batch.alter_column('cached', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('callrecord', 'cached')
