"""columns that hold a call record id become BIGINT, before the id itself does

Revision ID: 0122
Revises: 0121
Create Date: 2026-11-18

`callrecord.id` moves to 64 bits in 0123 and the swap revision after it. Every column that stores one of its ids must
hold the larger value first, or the first id past 2,147,483,647 fails that column's writer:

- `callusagecursor.cursor_id`, `endpointstatcursor.cursor_id`, `shadowcursor.cursor_id`
- `webarenacallcursor.call_id`, `arenainsightstate.cursor`
- `webarenaseedprogress`: `first_id`, `highwater_id`, `last_id`
- `shadowpair.call_id`

`showcasecursor.through_id` is BIGINT already. The cursor tables hold one row or a few, and
`shadowpair` holds a few days of one experiment, so each rewrite is short. `arenaobservation.id`
also stores a call record id, as the primary key of a large table; changing its type rewrites that
table, so it is not part of this revision.

On Postgres each table changes in one `ALTER TABLE` statement that commits on its own, so the
migration holds one table's lock at a time. A table that already has the target type is skipped,
so a retry after a lock timeout resumes where it stopped. `env.py`'s session `lock_timeout` makes
each statement fail fast under contention.

Rollback floor: code older than this revision runs on BIGINT columns unchanged, so a code rollback
does not need the downgrade. The downgrade works only while every value still fits in `integer`;
after that it fails with "integer out of range" and changes nothing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0122"
down_revision: str | Sequence[str] | None = "0121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # column type change; see the rollback floor note

_COLUMNS: dict[str, tuple[str, ...]] = {
    "callusagecursor": ("cursor_id",),
    "endpointstatcursor": ("cursor_id",),
    "shadowcursor": ("cursor_id",),
    "webarenacallcursor": ("call_id",),
    "arenainsightstate": ("cursor",),
    "webarenaseedprogress": ("first_id", "highwater_id", "last_id"),
    "shadowpair": ("call_id",),
}

_TYPES = sa.text(
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = :table")


def _retype(old: type[sa.types.TypeEngine], new: type[sa.types.TypeEngine], sql_type: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for table, columns in _COLUMNS.items():
                types = dict(bind.execute(_TYPES, {"table": table}).all())
                pending = [name for name in columns if types.get(name) != sql_type.lower()]
                if not pending:
                    continue
                clauses = ", ".join(f'ALTER COLUMN "{name}" TYPE {sql_type}' for name in pending)
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
