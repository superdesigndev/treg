"""composite (org_id, id) on callrecord, and a partial one over its local runs - a team's
newest rows stop costing a walk of the whole table

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-30

Two questions of `callrecord` are "this team's newest N, older than a cursor":

- `/calls`: `org_id = ? AND kind <> 'async_poll' ORDER BY id DESC LIMIT n`, paged by `before_id`.
  No index carried `(org_id, id)`, so the planner walked the primary key backward and tested
  `org_id` row by row. For a busy team that is tens of thousands of foreign rows per page; for a
  quiet team whose newest rows are old, it is most of the table.
- Local runs (`/runs`, and the Activity feed's runs): `org_id = ? AND kind = 'local_run' AND
  method = 'GRANT'`, newest first. They are a sliver of a team's rows, so the planner read the
  team's WHOLE history through `ix_callrecord_org_id` and sorted the survivors.

`(org_id, id)` makes the first a backward range over one team. The second gets
`(org_id, created_at, id) WHERE kind = 'local_run'`, in the feed's time order, which stays small
however many calls a team makes. The Activity feed's calls read the existing
`(org_id, created_at)`. The single-column `ix_callrecord_org_id` stays: dropping it is a
separate, non-additive change.

Built with the 0020/0021 discipline (CONCURRENTLY in an autocommit block, INVALID debris dropped
first). The expand-safety linter counts the autocommit escape as non-additive, so this revision
declares a rollback floor pro forma: the operations are two additive indexes.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | Sequence[str] | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operations are additive indexes

_TABLE = "callrecord"
_LOCAL_RUN = "kind = 'local_run'"
_INDEXES = (
    ("ix_callrecord_org_id_id", ["org_id", "id"], None),
    ("ix_callrecord_org_local_run", ["org_id", "created_at", "id"], _LOCAL_RUN),
)

# Same values and reasoning as 0021; `env.py`'s values are restored before the block ends.
_LOCK_TIMEOUT = "180s"
_STATEMENT_TIMEOUT = "600s"
_ENV_LOCK_TIMEOUT = "5s"
_ENV_STATEMENT_TIMEOUT = "120s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name")


def _where(predicate: str | None) -> dict:
    if predicate is None:
        return {}
    return {"postgresql_where": sa.text(predicate), "sqlite_where": sa.text(predicate)}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        for name, columns, predicate in _INDEXES:  # SQLite: no concurrent mode, no traffic to block
            op.create_index(name, _TABLE, columns, **_where(predicate))
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'"))
        bind.execute(sa.text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        try:
            for name, columns, predicate in _INDEXES:
                valid = bind.execute(_VALIDITY, {"name": name}).scalar()
                if valid is True:
                    continue
                if valid is False:  # debris from a killed build - unusable, and never repaired
                    op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True)
                op.create_index(name, _TABLE, columns, postgresql_concurrently=True,
                                **_where(predicate))
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    for name, _, _ in _INDEXES:
        op.drop_index(name, table_name=_TABLE)
