"""The archive erasure queue (additive).

Revision ID: 0094
Revises: 0093

One row per request to erase what a team stored in the archive. The worker command
`treg-worker admin erase-pending` claims and runs them in small batches. A new, empty table, so
the migration takes no lock on a live table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0094"
down_revision: str | Sequence[str] | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "archiveerasurerequest"
_OPEN = "status IN ('pending', 'running')"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("include_public", sa.Boolean(), nullable=False),
        sa.Column("reference", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("cursor", sa.String(), nullable=False),
        sa.Column("keys", sa.Integer(), nullable=False),
        sa.Column("snapshots", sa.Integer(), nullable=False),
        sa.Column("objects", sa.Integer(), nullable=False),
        sa.Column("marks", sa.Integer(), nullable=False),
        sa.Column("kept_shared", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(op.f("ix_archiveerasurerequest_org_id"), _TABLE, ["org_id"])
    op.create_index("ix_archiveerasure_status_id", _TABLE, ["status", "id"])
    op.create_index("uq_archiveerasure_open_org", _TABLE, ["org_id"], unique=True,
                    postgresql_where=sa.text(_OPEN), sqlite_where=sa.text(_OPEN))


def downgrade() -> None:
    op.drop_index("uq_archiveerasure_open_org", table_name=_TABLE)
    op.drop_index("ix_archiveerasure_status_id", table_name=_TABLE)
    op.drop_index(op.f("ix_archiveerasurerequest_org_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
