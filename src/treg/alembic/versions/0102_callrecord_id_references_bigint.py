"""callrecord's id sequence and the cursors that store its ids become BIGINT

Revision ID: 0102
Revises: 0101
Create Date: 2026-11-13

`callrecord.id` is a Postgres `integer` fed by `callrecord_id_seq`, both capped at 2,147,483,647.
Moving the table itself to `bigint` is a separate, online migration (a copy into a new table), so
this revision only removes the other caps first:

- the sequence (`ALTER SEQUENCE ... AS bigint` changes metadata only; its maximum follows the type)
- the columns that remember a `callrecord.id`: the cursors of the catalog stats, usage rollup,
  shadow, Arena and Web Arena runs, and `shadowpair.call_id`

These tables are small, so each rewrite takes seconds. `arenaobservation.id` also holds a
`callrecord.id`, but as the primary key of a large table, so it moves with the online migration.

On Postgres each table changes in one `ALTER TABLE` statement and commits on its own, so it is
rewritten once and holds one table's lock at a time. A table or sequence that already has the
target type is skipped, so a retry after a lock timeout resumes where it stopped.

Rollback floor: code older than this revision runs on BIGINT columns and a BIGINT sequence
unchanged, so a code rollback does not need the downgrade. The downgrade works only while every
value still fits in `integer`; past that it fails with "integer out of range" and changes nothing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0102"
down_revision: str | Sequence[str] | None = "0101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # column and sequence type change; see the rollback floor note

_COLUMNS: dict[str, tuple[str, ...]] = {  # every column is NOT NULL
    "endpointstatcursor": ("cursor_id",),
    "callusagecursor": ("cursor_id",),
    "shadowcursor": ("cursor_id",),
    "shadowpair": ("call_id",),
    "arenainsightstate": ("cursor",),
    "webarenacallcursor": ("call_id",),
    "webarenaseedprogress": ("first_id", "highwater_id", "last_id"),
}

_TYPES = sa.text(
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = :table")
_SEQUENCE = sa.text(
    "SELECT s.seqrelid::regclass::text, format_type(s.seqtypid, NULL) FROM pg_sequence s "
    "WHERE s.seqrelid = pg_get_serial_sequence('callrecord', 'id')::regclass")


def _retype(old: type[sa.types.TypeEngine], new: type[sa.types.TypeEngine], sql_type: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            sequence = bind.execute(_SEQUENCE).first()
            if sequence is not None and sequence[1] != sql_type.lower():
                bind.execute(sa.text(f"ALTER SEQUENCE {sequence[0]} AS {sql_type}"))
            for table, columns in _COLUMNS.items():
                types = dict(bind.execute(_TYPES, {"table": table}).all())
                pending = [name for name in columns if types.get(name) != sql_type.lower()]
                if not pending:
                    continue
                clauses = ", ".join(f"ALTER COLUMN {name} TYPE {sql_type}" for name in pending)
                bind.execute(sa.text(f"ALTER TABLE {table} {clauses}"))
        return
    for table, columns in _COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            for name in columns:
                batch.alter_column(name, existing_type=old(), type_=new(), existing_nullable=False)


def upgrade() -> None:
    _retype(sa.Integer, sa.BigInteger, "BIGINT")


def downgrade() -> None:
    _retype(sa.BigInteger, sa.Integer, "INTEGER")
