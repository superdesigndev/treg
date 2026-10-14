"""composite (org_id, created_at) on ledgerentry — the per-call daily-cap check stops scanning the
whole platform's day

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-06

`ledger.spent_today` runs inside EVERY metered call's reserve transaction, on an api-pool
connection: `sum(amount_micro) WHERE org_id = ? AND kind = 'settle' AND created_at >= today`.
`ledgerentry` carried only single-column indexes, so the planner had two bad choices and took one
per org: walk `ix_ledgerentry_created_at` over the WHOLE platform's day and filter `org_id` in
memory (heavy orgs), or BitmapAnd the org's ENTIRE history against the day (light orgs). Measured
on prod 2026-09-06 at 4.38M rows / 2.3 GB, with ~400k rows written per day:

    heaviest org, warm cache:  Rows Removed by Filter: 322,638
                               Buffers: shared hit=367,715 read=13,777    (539 ms)
    same query, cold cache:    56–106 s, holding an api-pool slot the whole time

    ix_ledgerentry_org_id       1,858,056 scans    90,870,787,481 tuples read
    ix_ledgerentry_created_at   1,295,504 scans   157,269,530,757 tuples read
    ledgerentry heap            6.5 BILLION blocks read — 4× `callrecord`, the largest consumer

That is the API-pool saturation. The database is a 1 vCPU / 2 GB instance with a 512 MB buffer
cache in front of 35 GB; a 30-second activity sample showed 88 % of active backends in
`IO DataFileRead` / `IPC BufferIO` (waiting for a page, or for ANOTHER backend reading the same
page) and 674 of 879 active samples were this one query. Whenever a large scan evicts the day's
ledger pages, every in-flight `spent_today` stalls together on disk for tens of seconds, each one
holding an api-pool connection, and the pool empties into `503 treg_saturated`. Raising the pool
(15 → 20 on 2026-09-05) made it worse: more concurrent readers of the same cold pages.

`(org_id, created_at)` turns both plans into one tight range: the org's rows since midnight,
nothing else. It also serves `ledger.entries_of` (the `/billing` page), which walked the whole
`created_at` index BACKWARD filtering `org_id` — measured 57 s for a quiet org. `kind` is
deliberately not a third column: the pair serves both queries, a triple would serve only one, and
every extra index on a 400k-rows/day table is paid on every write.

Built with the 0020 discipline — see that revision's docstring for why raising `lock_timeout` for
a CONCURRENT build does not break the 2026-08-15 rule (its lock conflicts with neither reads nor
writes and a statement waiting for it blocks nobody), and why each index is inspected for INVALID
debris before building. The expand-safety linter counts the autocommit escape as non-additive, so
this revision declares a rollback floor pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma — see the rollback floor note; the operation is one additive index

_TABLE = "ledgerentry"
_INDEXES = (
    ("ix_ledgerentry_org_id_created_at", ["org_id", "created_at"]),
)

# Long enough to outlast an autovacuum pass on a 2.3 GB table; see 0020 for why waiting on THIS
# lock is safe. `env.py`'s values are restored before the autocommit block ends.
_LOCK_TIMEOUT = "180s"
_STATEMENT_TIMEOUT = "600s"
_ENV_LOCK_TIMEOUT = "5s"
_ENV_STATEMENT_TIMEOUT = "120s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        for name, columns in _INDEXES:  # SQLite: no concurrent mode, no traffic to block
            op.create_index(name, _TABLE, columns)
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'"))
        bind.execute(sa.text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        try:
            for name, columns in _INDEXES:
                valid = bind.execute(_VALIDITY, {"name": name}).scalar()
                if valid is True:
                    continue
                if valid is False:  # debris from a killed build — unusable, and never repaired
                    op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True)
                op.create_index(name, _TABLE, columns, postgresql_concurrently=True)
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    for name, _ in _INDEXES:
        op.drop_index(name, table_name=_TABLE)
