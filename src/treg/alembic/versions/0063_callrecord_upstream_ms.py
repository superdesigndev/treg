"""callrecord.upstream_ms: the provider's share of a call's duration

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-05

`duration_ms` covers everything treg does for a call, including a token refresh that makes its own
network request, so a slow call could not be pinned on the provider or on treg. One nullable column,
no default and no backfill: the ALTER only takes its brief lock on a hot table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | Sequence[str] | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("callrecord", sa.Column("upstream_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("callrecord") as batch:
        batch.drop_column("upstream_ms")
