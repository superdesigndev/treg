"""org, invite, denyrule, secret, tool: build the baseline indexes a stamped database never got,
and a partial index on org.stripe_default_pm

Revision ID: 0116
Revises: 0115
Create Date: 2026-11-19

The model and revision 0001 declare five single-column indexes that a database predating Alembic
does not have: their columns came from the old `init_db` `ALTER TABLE ... ADD COLUMN`, which adds no
index, and the database was then only stamped at the baseline (the same gap 0072 closed for
`callrecord.call_ref`). There a lookup by Stripe customer reads the whole `org` table, and deleting a
project reads all of `tool` and `denyrule` for its foreign key. A database Alembic created already
has them, and this revision leaves them alone.

`ix_org_stripe_default_pm` is new everywhere: Stripe's payment-method events name only the card,
so the org is found by it. Partial (`IS NOT NULL`), because most teams never save a card.

PostgreSQL checks every name first and refuses to touch a same-named index with another definition,
then builds each missing one with CREATE INDEX CONCURRENTLY IF NOT EXISTS outside a transaction, so
writes continue. An INVALID index left by an interrupted concurrent build is dropped and rebuilt;
IF NOT EXISTS alone would keep it forever. The lock and statement timeouts are raised for these
builds only and restored after, like 0091. SQLite: plain CREATE INDEX IF NOT EXISTS.

Rollback floor (pro forma, like 0072): the expand-safety linter counts the INVALID-index drop as
non-additive. Downgrade drops only `ix_org_stripe_default_pm`: 0001 owns the other five, and
dropping them here would take them from databases that always had them.
"""
from collections.abc import Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "0116"
down_revision: str | Sequence[str] | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma, see the rollback floor note: the operations add indexes

_PARTIAL = "ix_org_stripe_default_pm"

# table, column, index, predicate in the form pg_get_expr prints it (None: a full index)
_INDEXES = (
    ("org", "stripe_customer_id", "ix_org_stripe_customer_id", None),
    ("invite", "email_token_hash", "ix_invite_email_token_hash", None),
    ("denyrule", "project_id", "ix_denyrule_project_id", None),
    ("secret", "provider", "ix_secret_provider", None),
    ("tool", "project_id", "ix_tool_project_id", None),
    ("org", "stripe_default_pm", _PARTIAL, "(stripe_default_pm IS NOT NULL)"),
)

# The index of that name in this schema, whether it is valid, and whether it is the expected one:
# a plain non-unique btree on `table(column)` alone, with exactly the expected predicate.
_STATE = sa.text("""
    SELECT ix.indisvalid AS valid,
           t.relname = :table AND ix.indnatts = 1 AND a.attname = :column
           AND NOT ix.indisunique AND ix.indexprs IS NULL AND am.amname = 'btree'
           AND pg_get_expr(ix.indpred, ix.indrelid) IS NOT DISTINCT FROM CAST(:predicate AS text)
           AS expected
    FROM pg_class i
    JOIN pg_namespace n ON n.oid = i.relnamespace
    JOIN pg_index ix ON ix.indexrelid = i.oid
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_am am ON am.oid = i.relam
    LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
    WHERE n.nspname = current_schema() AND i.relname = :name
""")


def _create(table: str, column: str, name: str, predicate: str | None, concurrently: bool) -> str:
    where = f" WHERE {predicate}" if predicate else ""
    how = "CONCURRENTLY " if concurrently else ""
    return f"CREATE INDEX {how}IF NOT EXISTS {name} ON {table} ({column}){where}"


@contextmanager
def _concurrent():
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default. Restore the
    # caller's actual settings even when a build times out.
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
    if bind.dialect.name != "postgresql":
        for table, column, name, predicate in _INDEXES:
            bind.execute(sa.text(_create(table, column, name, predicate, concurrently=False)))
        return
    states = {}
    for table, column, name, predicate in _INDEXES:
        state = bind.execute(_STATE, {"table": table, "column": column, "name": name,
                                      "predicate": predicate}).mappings().one_or_none()
        if state is not None and state["expected"] is not True:
            raise RuntimeError(f"Refusing index migration: {name} exists but is not the expected index")
        states[name] = state
    missing = [index for index in _INDEXES if not (states[index[2]] and states[index[2]]["valid"])]
    if not missing:
        return
    with _concurrent() as bind:
        for table, column, name, predicate in missing:
            if states[name] is not None:   # left INVALID by an interrupted concurrent build
                op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
            bind.execute(sa.text(_create(table, column, name, predicate, concurrently=True)))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.drop_index(_PARTIAL, table_name="org", if_exists=True)
        return
    with _concurrent():
        op.drop_index(_PARTIAL, table_name="org", postgresql_concurrently=True, if_exists=True)
