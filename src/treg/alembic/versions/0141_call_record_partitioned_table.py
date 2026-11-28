"""an empty call record table, partitioned by month, with a 64-bit id

Revision ID: 0141
Revises: 0140
Create Date: 2026-11-18

`callrecord` cannot change in place: `ALTER COLUMN id TYPE bigint` rewrites the whole table under
an ACCESS EXCLUSIVE lock, and a plain table can only be emptied with `DELETE`, which never gives
its space back. This revision builds the table that replaces it and touches no row; a later revision swaps
the two.

`callrecord_partitioned` has the columns of `callrecord` (`LIKE`, so a database whose column
order differs from the baseline gets its own order back), with `id` as `bigint`, and is
partitioned by month on `created_at`. It has no partition yet; the backfill creates the ones it needs.

- **Primary key (id, created_at).** A unique constraint on a partitioned table must include the
  partition column. `id` alone still names one row, because every id comes from one sequence.
- **One sequence.** The new table draws from the sequence `callrecord` already uses, and that
  sequence becomes `bigint`. Ids continue from where they are, so every rollup cursor stays valid.
  `ALTER SEQUENCE ... AS bigint` changes the sequence's catalog row only.
- **Indexes** are declared on the partitioned table, which holds no data, so nothing is built
  now. Postgres builds each one on a partition when the partition is created, empty.
- **No foreign key to `org`.** Team deletion removes a team's call records itself, and a foreign
  key would take a lock on the `org` row for every audit insert.

Other databases are left alone: SQLite's `INTEGER PRIMARY KEY` is 64-bit already and it has no
partitions. PostgreSQL older than 14 is refused, because dropping a month without blocking readers
needs `DETACH PARTITION ... CONCURRENTLY`.

Rollback floor (pro forma): nothing reads or writes the new table before the swap, so older code runs
unchanged. The linter counts the raw statements as non-additive. The downgrade drops the table and
leaves the sequence `bigint`, which the `integer` column accepts.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0141"
down_revision: str | Sequence[str] | None = "0140"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma, see the rollback floor note: the operations add a table

NEW = "callrecord_partitioned"

# name suffix -> definition after "ON <table>"; the model's indexes at this revision.
INDEXES: dict[str, str] = {
    "cached_true": "(cached) WHERE cached",
    "call_ref": "(call_ref)",
    "endpoint_id_created_at": "(endpoint_id, created_at)",
    "endpoint_id_id": "(endpoint_id, id)",
    "org_id_created_at": "(org_id, created_at)",
    "org_id_id": "(org_id, id)",
    "org_id_user_email_created_at": "(org_id, user_email, created_at)",
    "org_key_id": "(org_id, api_key_id, id) WHERE api_key_id IS NOT NULL",
    "org_local_run": "(org_id, created_at, id) WHERE kind = 'local_run'",
    "tool_name": "(tool_name)",
    "user_email": "(user_email)",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(sa.text("SELECT current_setting('server_version_num')::int")).scalar_one() < 140000:
        raise RuntimeError("The partitioned call record table needs PostgreSQL 14 or newer "
                           "(DETACH PARTITION ... CONCURRENTLY). Nothing was changed.")
    sequence = bind.execute(sa.text("SELECT pg_get_serial_sequence('callrecord', 'id')")).scalar_one()
    if sequence is None:
        raise RuntimeError("callrecord.id has no owned sequence; refusing to build its replacement")
    op.execute(f"ALTER SEQUENCE {sequence} AS bigint")
    op.execute(f"CREATE TABLE {NEW} (LIKE callrecord INCLUDING DEFAULTS) "
               "PARTITION BY RANGE (created_at)")
    op.execute(f"ALTER TABLE {NEW} ALTER COLUMN id TYPE bigint")
    op.execute(f"ALTER TABLE {NEW} ADD CONSTRAINT {NEW}_pkey PRIMARY KEY (id, created_at)")
    for suffix, definition in INDEXES.items():
        op.execute(f"CREATE INDEX ix_{NEW}_{suffix} ON {NEW} {definition}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP TABLE IF EXISTS {NEW}")
