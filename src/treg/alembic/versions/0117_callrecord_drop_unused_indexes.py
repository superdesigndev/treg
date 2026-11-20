"""callrecord: drop six single-column indexes no query uses

Revision ID: 0117
Revises: 0116
Create Date: 2026-11-19

0001 declares single-column indexes on `endpoint_id`, `provider`, `client`, `params_hash`,
`budget_val` and `refused_by`. No query is served by them: `endpoint_id` lookups use the composites
that lead with it (`ix_callrecord_endpoint_id_created_at`, `ix_callrecord_endpoint_id_id`); the
provider filters of the admin routes walk the primary key on purpose; reports filter by time and
group by the rest, or read rollups; `refused_by IS NULL` matches most rows. On the audit table each
one only costs disk and a write on every insert. A database stamped at the baseline over a
pre-Alembic schema never had them (see 0116) and serves the same queries without them, so this
revision makes every database match it.

PostgreSQL checks every name first and refuses to drop a same-named index with another definition,
then uses DROP INDEX CONCURRENTLY IF EXISTS outside a transaction, like 0091. Each drop waits for
the transactions already using the index; where none exist (the stamped databases), the revision
only records itself. SQLite: plain DROP INDEX IF EXISTS.

Rollback floor: 0117 is a contract revision because it removes indexes. Application queries remain
compatible; downgrade recreates the indexes concurrently on PostgreSQL before returning to 0116.
Rebuilding them scans the whole audit table and needs disk space, so it takes far longer than
removing them.
"""
from collections.abc import Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "0117"
down_revision: str | Sequence[str] | None = "0116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True

_TABLE = "callrecord"
# column, index
_INDEXES = (
    ("endpoint_id", "ix_callrecord_endpoint_id"),
    ("provider", "ix_callrecord_provider"),
    ("client", "ix_callrecord_client"),
    ("params_hash", "ix_callrecord_params_hash"),
    ("budget_val", "ix_callrecord_budget_val"),
    ("refused_by", "ix_callrecord_refused_by"),
)

# The index of that name in this schema, whether it is valid, and whether it is the one 0001
# declares: a plain non-unique btree on `callrecord(column)` alone.
_STATE = sa.text("""
    SELECT ix.indisvalid AS valid,
           t.relname = 'callrecord' AND ix.indnatts = 1 AND a.attname = :column
           AND NOT ix.indisunique AND ix.indexprs IS NULL AND ix.indpred IS NULL
           AND am.amname = 'btree' AS expected
    FROM pg_class i
    JOIN pg_namespace n ON n.oid = i.relnamespace
    JOIN pg_index ix ON ix.indexrelid = i.oid
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_am am ON am.oid = i.relam
    LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
    WHERE n.nspname = current_schema() AND i.relname = :name
""")


def _checked(bind) -> dict[str, bool | None]:
    """Each index's state (None absent, else valid), failing before any change on a stranger."""
    states: dict[str, bool | None] = {}
    for column, name in _INDEXES:
        if bind.dialect.name == "postgresql":
            state = bind.execute(_STATE, {"column": column, "name": name}).mappings().one_or_none()
            if state is not None and state["expected"] is not True:
                raise RuntimeError(f"Refusing index migration: {name} is not callrecord({column})")
            states[name] = None if state is None else bool(state["valid"])
        else:
            found = next((i for i in sa.inspect(bind).get_indexes(_TABLE) if i["name"] == name), None)
            if found is not None and (found["unique"] or found["column_names"] != [column]
                                      or found.get("dialect_options")):
                raise RuntimeError(f"Refusing index migration: {name} is not callrecord({column})")
            states[name] = None if found is None else True
    return states


@contextmanager
def _concurrent():
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default. Like 0091, this
    # can wait for maintenance without blocking DML. Restore the caller's actual settings even when
    # a drop or build times out.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        settings = {name: bind.execute(sa.text(f"SHOW {name}")).scalar_one()
                    for name in ("lock_timeout", "statement_timeout")}
        try:
            bind.execute(sa.text("SET lock_timeout = '180s'"))
            bind.execute(sa.text("SET statement_timeout = '600s'"))
            yield bind
        finally:
            for name, value in settings.items():
                bind.execute(sa.text("SELECT set_config(:name, :value, false)"),
                             {"name": name, "value": value})


def upgrade() -> None:
    bind = op.get_bind()
    states = _checked(bind)
    present = [name for _, name in _INDEXES if states[name] is not None]
    if not present:
        return
    if bind.dialect.name != "postgresql":
        for name in present:
            op.drop_index(name, table_name=_TABLE, if_exists=True)
        return
    with _concurrent():
        for name in present:
            op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True, if_exists=True)


def downgrade() -> None:
    bind = op.get_bind()
    states = _checked(bind)
    if bind.dialect.name != "postgresql":
        for column, name in _INDEXES:
            if states[name] is None:
                op.create_index(name, _TABLE, [column])
        return
    with _concurrent():
        for column, name in _INDEXES:
            if states[name] is True:
                continue
            if states[name] is False:   # left INVALID by an interrupted concurrent build
                op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True)
            op.create_index(name, _TABLE, [column], postgresql_concurrently=True, if_not_exists=True)
