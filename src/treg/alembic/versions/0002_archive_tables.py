"""archive tables — archivekey + archivesnapshot (PR 1 of the cache/archive; treg/archive.py)

Two tables, no writers yet: the recorder arrives in the next PR, so upgrading is pure shape.
Bodies live in Postgres on purpose — the IdempotentCall precedent (a paid answer worth keeping is
already stored there). Downgrade drops both; nothing else references them.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-27

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'archivekey',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('endpoint_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('policy', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('ttl_s', sa.Integer(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.Column('change_seen', sa.Integer(), nullable=False),
        sa.Column('stable_seen', sa.Integer(), nullable=False),
        sa.Column('last_changed_at', sa.DateTime(), nullable=True),
        sa.Column('volatile_paths', sa.JSON(), nullable=True),
        sa.Column('heat', sa.Float(), nullable=False),
        sa.Column('last_requested_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key_hash', name='uq_archive_key_hash'),
    )
    op.create_index(op.f('ix_archivekey_key_hash'), 'archivekey', ['key_hash'], unique=False)
    op.create_index(op.f('ix_archivekey_endpoint_id'), 'archivekey', ['endpoint_id'], unique=False)
    op.create_index(op.f('ix_archivekey_provider'), 'archivekey', ['provider'], unique=False)
    op.create_index(op.f('ix_archivekey_fetched_at'), 'archivekey', ['fetched_at'], unique=False)

    op.create_table(
        'archivesnapshot',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('media_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('content_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('body', sa.LargeBinary(), nullable=True),
        sa.Column('body_of', sa.Integer(), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.Column('origin', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['key_id'], ['archivekey.id']),
        sa.ForeignKeyConstraint(['body_of'], ['archivesnapshot.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key_id', 'version', name='uq_archive_snapshot_version'),
    )
    op.create_index(op.f('ix_archivesnapshot_key_id'), 'archivesnapshot', ['key_id'], unique=False)
    op.create_index(op.f('ix_archivesnapshot_fetched_at'), 'archivesnapshot', ['fetched_at'], unique=False)
    op.create_index('ix_archive_snapshot_content', 'archivesnapshot', ['content_hash'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_archive_snapshot_content', table_name='archivesnapshot')
    op.drop_index(op.f('ix_archivesnapshot_fetched_at'), table_name='archivesnapshot')
    op.drop_index(op.f('ix_archivesnapshot_key_id'), table_name='archivesnapshot')
    op.drop_table('archivesnapshot')
    op.drop_index(op.f('ix_archivekey_fetched_at'), table_name='archivekey')
    op.drop_index(op.f('ix_archivekey_provider'), table_name='archivekey')
    op.drop_index(op.f('ix_archivekey_endpoint_id'), table_name='archivekey')
    op.drop_index(op.f('ix_archivekey_key_hash'), table_name='archivekey')
    op.drop_table('archivekey')
