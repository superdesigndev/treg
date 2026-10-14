"""archivesnapshot.origin_org_id: partial index for the team's own snapshots

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-10

Erasing a team's archive (docs/context/architecture/archive.md, "Opting out") starts from "which
keys hold a snapshot this team fetched on its own credential", and every team deletion runs that
query through the cascade, the sandbox reaper included. Without an index it is a scan of the whole
snapshot table per deleted team. Partial, because the column is NULL on every platform-key
snapshot, which is most of the table. Built CONCURRENTLY on Postgres, like 0016: the preDeploy
step runs while the old build still serves traffic, and a concurrent build never blocks the
recorder. SQLite builds it plainly.

The expand-safety linter counts the autocommit escape as non-additive, so this revision declares
a rollback floor pro forma: the operation is an index, and downgrading past it merely drops it.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: str | Sequence[str] | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma — see the rollback floor note; the operation is an additive index

_NAME = "ix_archivesnapshot_origin_org"


def upgrade() -> None:
    where = sa.text("origin_org_id IS NOT NULL")
    if op.get_bind().dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
        with op.get_context().autocommit_block():
            op.create_index(_NAME, "archivesnapshot", ["origin_org_id"],
                            postgresql_where=where, postgresql_concurrently=True)
    else:
        op.create_index(_NAME, "archivesnapshot", ["origin_org_id"], sqlite_where=where)


def downgrade() -> None:
    op.drop_index(_NAME, table_name="archivesnapshot")
