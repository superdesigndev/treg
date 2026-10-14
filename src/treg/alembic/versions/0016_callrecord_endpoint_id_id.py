"""composite index (endpoint_id, id) on callrecord — the panel's event feed stops table-walking

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-03

"This endpoint's newest 30 calls" had no matching index, so the planner walked the whole call
table backward via the primary key, testing endpoint_id row by row — measured 7.5s per panel
click on prod. Built CONCURRENTLY on Postgres: the preDeploy step runs while the old build still
serves traffic, and a concurrent build never blocks writes to the hot audit table. SQLite (tests,
local) builds it plainly — it has no concurrent mode and no traffic to block.

The expand-safety linter counts the autocommit escape (get_bind/get_context) as non-additive, so
this revision declares a rollback floor pro forma: the operation itself is purely additive — an
index — and downgrading past it merely drops the index.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma — see the rollback floor note; the operation is an additive index


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
        with op.get_context().autocommit_block():
            op.create_index("ix_callrecord_endpoint_id_id", "callrecord",
                            ["endpoint_id", "id"], postgresql_concurrently=True)
    else:
        op.create_index("ix_callrecord_endpoint_id_id", "callrecord", ["endpoint_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_callrecord_endpoint_id_id", table_name="callrecord")
