"""archivekey request shape — req_method/req_url/req_body (PR 5; the refresh worker's memory)

The worker can only re-ask a question that was written down. This is the PRE-INJECTION request:
method, vendor-facing URL (fixed upstream + forwarded caller params), and the caller body.
Credentials cannot appear — injection happens inside the relay, after this shape is fixed.
Backfill is empty strings/NULL: keys recorded before this migration simply cannot be refreshed
until a caller asks their question again, which re-stores the shape.

Rollback floor: the new NOT NULL columns keep no server default once backfilled, so code older
than this revision can no longer insert archive rows.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-27

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '0004'
down_revision: str | Sequence[str] | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('archivekey', sa.Column('req_method', sqlmodel.sql.sqltypes.AutoString(),
                                          nullable=False, server_default=''))
    op.add_column('archivekey', sa.Column('req_url', sqlmodel.sql.sqltypes.AutoString(),
                                          nullable=False, server_default=''))
    op.add_column('archivekey', sa.Column('req_body', sa.LargeBinary(), nullable=True))
    op.add_column('archivekey', sa.Column('req_headers', sa.JSON(), nullable=True))
    with op.batch_alter_table('archivekey') as batch:
        batch.alter_column('req_method', server_default=None)
        batch.alter_column('req_url', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('archivekey', 'req_headers')
    op.drop_column('archivekey', 'req_body')
    op.drop_column('archivekey', 'req_url')
    op.drop_column('archivekey', 'req_method')
