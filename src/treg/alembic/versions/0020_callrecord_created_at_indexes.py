"""composite (endpoint_id, created_at) and (org_id, created_at) on callrecord — time windows stop
reading whole histories

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-06

Every question asked of `callrecord` is "… since <time>", and no index carried `created_at`. The
planner therefore chose an index for the other column and filtered the date in memory, reading an
endpoint's or an org's entire history to answer a bounded time-window question. The catalog
observation refresh and per-member daily counts both become tight range scans with these pairs.

This is the fix for the API-pool saturation, not a pool size: the three pools bulkhead CONNECTIONS,
not the single database's CPU, so a scan of this table makes every ordinary 3 ms request query queue
behind it until `pool_timeout` fires and callers get `503 treg_saturated`.

**Why this one raises `lock_timeout`, and why that is not the 2026-08-15 rule being broken.** The
first attempt died on `LockNotAvailableError: canceling statement due to lock timeout` in 5 s — not
against traffic, which cannot block it, but almost certainly against autovacuum, which runs
constantly on a table this size and takes the SAME `SHARE UPDATE EXCLUSIVE` lock. The 5 s floor in
`alembic/env.py` exists because an **ALTER** takes `ACCESS EXCLUSIVE`: it queues behind live traffic
and then every new query queues behind IT, which is exactly how the 2026-08-15 outage wedged the
database. `CREATE INDEX CONCURRENTLY` is not in that class — its lock conflicts with neither
`SELECT` nor `INSERT`/`UPDATE`/`DELETE`, it blocks no reads or writes while it builds, and a
statement WAITING for it holds nothing and blocks nobody. Waiting longer here costs a slower
pre-deploy and nothing else. The timeouts are restored to `env.py`'s values before the block ends.

**Idempotent per index, because a concurrent build can leave debris.** A CIC that is killed
mid-flight leaves an INVALID index — present, so a naive `IF NOT EXISTS` would skip it forever and
leave the scan in place with nothing failing. Each index is therefore inspected first: valid ⇒ skip
(a re-run, or a build done out of band), invalid ⇒ drop it concurrently and rebuild, absent ⇒ build.
A failed attempt costs only the indexes it had not reached.

The expand-safety linter counts the autocommit escape (get_bind/get_context) as non-additive, so
this revision declares a rollback floor pro forma, as 0016 did: the operations themselves are purely
additive — two indexes — and downgrading past it merely drops them.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma — see the rollback floor note; the operations are additive indexes

_INDEXES = (
    ("ix_callrecord_endpoint_id_created_at", ["endpoint_id", "created_at"]),
    ("ix_callrecord_org_id_created_at", ["org_id", "created_at"]),
)

# Long enough to outlast an autovacuum pass on a 1.7 GB table; see the docstring for why waiting
# on THIS lock is safe. `env.py`'s values are restored before the autocommit block ends.
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
            op.create_index(name, "callrecord", columns)
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
                    op.drop_index(name, table_name="callrecord", postgresql_concurrently=True)
                op.create_index(name, "callrecord", columns, postgresql_concurrently=True)
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    for name, _ in _INDEXES:
        op.drop_index(name, table_name="callrecord")
