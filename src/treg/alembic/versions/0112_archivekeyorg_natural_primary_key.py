"""archivekeyorg: (org_id, key_hash) becomes the primary key; the serial id retires

Revision ID: 0112
Revises: 0111
Create Date: 2026-11-18

A team's first paid call on a question writes its `archivekeyorg` mark inside the settle
transaction. With a serial `id` that insert takes the next number of a 32-bit sequence, and once
the sequence is exhausted every first-time settle rolls back and its answer goes unpaid (a routed
call writes all its children's marks together, so one refusal leaves the whole call unpaid).
Nothing reads `id`: the mark is found by (org_id, key_hash), which `uq_archive_key_org` already
keeps unique. This revision makes those two columns the primary key and stops `id` from being
assigned, so a mark needs no sequence at all.

PostgreSQL, one ALTER TABLE in this revision's transaction: drop the `id` primary key, drop the
`id` default and NOT NULL, drop `uq_archive_key_org`, and attach 0111's unique index as the new
primary key (`ADD PRIMARY KEY USING INDEX`, which renames the index and scans nothing because both
columns are already NOT NULL). Every step changes the catalog only; the table is not rewritten.
It takes an ACCESS EXCLUSIVE lock for the length of that catalog change. env.py's `lock_timeout`
makes it give up rather than queue settles behind it, and `maintenance` retries the upgrade, which
resumes at this revision.

`id` stays as a nullable column with its sequence still owned by it, so code older than this
revision keeps working during a rolling deploy: it still assigns `id` from the sequence and reads
it only as an existence probe, which a NULL `id` on a row new code wrote still satisfies.
Dropping the column and the sequence is a later contract revision, once no older code runs.

SQLite: the table is rebuilt with the new primary key (batch mode).

Rollback floor: none for code. Code older than this revision runs on this schema, as above, so a
code rollback does not need the downgrade. The downgrade numbers every row new code wrote from
the sequence, restores the `id` primary key and `uq_archive_key_org`, and builds both indexes
under the table lock, so it scans and locks the whole table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0112"
down_revision: str | Sequence[str] | None = "0111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # changes the primary key; see the rollback floor note

_INDEX = "ix_archivekeyorg_org_key_unique"   # built by 0111

_PRIMARY_KEY = sa.text("""
    SELECT c.conname AS name,
           array_agg(a.attname::text ORDER BY k.ord) AS columns
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
    WHERE n.nspname = current_schema() AND t.relname = 'archivekeyorg' AND c.contype = 'p'
    GROUP BY c.conname
""")


def _sqlite_shape(*, natural: bool) -> sa.Table:
    """The table after this revision (`natural`) or before it, for batch mode to rebuild into."""
    return sa.Table(
        "archivekeyorg", sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=not natural, nullable=natural),
        sa.Column("org_id", sa.Integer(), primary_key=natural, nullable=False),
        sa.Column("key_hash", sa.String(), primary_key=natural, nullable=False),
        sa.Column("first_call_at", sa.DateTime(), nullable=False),
        sa.Column("last_call_at", sa.DateTime(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        *(() if natural else (sa.UniqueConstraint("org_id", "key_hash", name="uq_archive_key_org"),)),
        sa.Index("ix_archivekeyorg_key_hash", "key_hash"),
    )


def _sqlite_upgrade() -> None:
    with op.batch_alter_table("archivekeyorg", recreate="always",
                              copy_from=_sqlite_shape(natural=True)):
        pass


def _sqlite_downgrade() -> None:
    op.execute(
        "UPDATE archivekeyorg SET id = (SELECT COALESCE(MAX(id), 0) FROM archivekeyorg) + rowid "
        "WHERE id IS NULL")
    with op.batch_alter_table("archivekeyorg", recreate="always",
                              copy_from=_sqlite_shape(natural=False)):
        pass


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _sqlite_upgrade()
        return
    primary = bind.execute(_PRIMARY_KEY).mappings().one_or_none()
    if primary is not None and list(primary["columns"]) == ["org_id", "key_hash"]:
        return
    valid = bind.execute(sa.text(
        "SELECT ix.indisvalid FROM pg_class i JOIN pg_index ix ON ix.indexrelid = i.oid "
        "JOIN pg_namespace n ON n.oid = i.relnamespace "
        "WHERE n.nspname = current_schema() AND i.relname = :name"), {"name": _INDEX}).scalar()
    if valid is not True:
        raise RuntimeError(f"Refusing primary key migration: {_INDEX} from 0111 is missing or invalid")
    drop_primary = f"DROP CONSTRAINT {primary['name']}, " if primary is not None else ""
    op.execute(
        "ALTER TABLE archivekeyorg "
        f"{drop_primary}"
        "DROP CONSTRAINT IF EXISTS uq_archive_key_org, "
        "ALTER COLUMN id DROP DEFAULT, "
        "ALTER COLUMN id DROP NOT NULL, "
        f"ADD CONSTRAINT archivekeyorg_pkey PRIMARY KEY USING INDEX {_INDEX}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _sqlite_downgrade()
        return
    op.execute("UPDATE archivekeyorg SET id = nextval('archivekeyorg_id_seq') WHERE id IS NULL")
    op.execute(
        "ALTER TABLE archivekeyorg "
        "DROP CONSTRAINT archivekeyorg_pkey, "
        "ALTER COLUMN id SET DEFAULT nextval('archivekeyorg_id_seq'::regclass), "
        "ALTER COLUMN id SET NOT NULL, "
        "ADD CONSTRAINT archivekeyorg_pkey PRIMARY KEY (id), "
        "ADD CONSTRAINT uq_archive_key_org UNIQUE (org_id, key_hash)")
