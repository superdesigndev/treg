"""callrecord.call_ref: build the index a database stamped at the baseline never got

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-13

The model and revision 0001 declare `ix_callrecord_call_ref`, so a database created by Alembic
has it. A database that predates Alembic does not: it got `call_ref` from the old `init_db`
`ALTER TABLE ... ADD COLUMN`, which adds no index, and was then only stamped at the baseline.
There every `call_ref` lookup reads the whole audit table: the spend rollup's key lookup
(`call_ref IN (...)`), `GET /calls/{call_ref}` and the async hit UPDATE in `audit.py`.

PostgreSQL builds it with CREATE INDEX CONCURRENTLY IF NOT EXISTS, outside a transaction, so the
recorder keeps inserting while it builds. On large tables that takes longer than the 120s
statement timeout env.py sets for every migration statement, so this statement alone runs
without one (the lock timeout stays bounded, like 0061), and both settings are restored after.
A previous concurrent build that was interrupted leaves an INVALID index of the same name, which
IF NOT EXISTS would keep forever: it is dropped and rebuilt. A valid index (every database
Alembic created) is left alone. SQLite: a plain CREATE INDEX IF NOT EXISTS.

Rollback floor (pro forma, like 0068): the expand-safety linter counts the autocommit block and
the invalid-index drop as non-additive. Downgrade is a no-op: on a fresh database 0001 owns this
index, and dropping it here would take it from databases that always had it.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | Sequence[str] | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma, see the rollback floor note: the operation adds an index

_NAME = "ix_callrecord_call_ref"

# The index of that name in this schema, whether it is valid, and whether it is the expected
# one: a plain btree on callrecord(call_ref) alone.
_STATE = sa.text("""
    SELECT ix.indisvalid AS valid,
           t.relname = 'callrecord' AND ix.indnatts = 1 AND a.attname = 'call_ref'
           AND ix.indexprs IS NULL AND ix.indpred IS NULL AND am.amname = 'btree' AS expected
    FROM pg_class i
    JOIN pg_namespace n ON n.oid = i.relnamespace
    JOIN pg_index ix ON ix.indexrelid = i.oid
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_am am ON am.oid = i.relam
    LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
    WHERE n.nspname = current_schema() AND i.relname = :name
""")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {_NAME} ON callrecord (call_ref)"))
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        state = bind.execute(_STATE, {"name": _NAME}).mappings().one_or_none()
        if state is not None and state["expected"] is not True:
            raise RuntimeError(f"Refusing index migration: {_NAME} exists but is not callrecord(call_ref)")
        if state is not None and state["valid"]:
            return
        settings = {name: bind.execute(sa.text(f"SHOW {name}")).scalar_one()
                    for name in ("lock_timeout", "statement_timeout")}
        try:
            bind.execute(sa.text("SET lock_timeout = '180s'"))
            bind.execute(sa.text("SET statement_timeout = 0"))   # this build only; restored below
            if state is not None:   # left INVALID by an interrupted concurrent build
                op.drop_index(_NAME, table_name="callrecord", postgresql_concurrently=True, if_exists=True)
            bind.execute(sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_NAME} ON callrecord (call_ref)"))
        finally:
            for name, value in settings.items():
                bind.execute(sa.text("SELECT set_config(:name, :value, false)"), {"name": name, "value": value})


def downgrade() -> None:
    # No-op on purpose: 0001 owns this index on every database Alembic created.
    pass
