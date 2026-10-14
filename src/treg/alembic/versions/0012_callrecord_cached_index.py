"""partial index over callrecord.cached — the archive report's two counts stop scanning

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-03

The archive panel's report counts cached=true rows twice per refresh over the biggest table in
the database; without an index that is two full scans per poll (measured 78s total report time on
prod at 425k archive keys). Partial — only the cached=true sliver is indexed, so the index stays
tiny and writes to the hot audit table pay almost nothing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_callrecord_cached_true", "callrecord", ["cached"],
                    postgresql_where=sa.text("cached"), sqlite_where=sa.text("cached"))


def downgrade() -> None:
    op.drop_index("ix_callrecord_cached_true", table_name="callrecord")
