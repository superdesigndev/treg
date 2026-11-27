"""tagspend: drop ix_tagspend_org_dim_val_created, covered by 0134's index

Revision ID: 0135
Revises: 0134
Create Date: 2026-11-26

0134 added `ix_tagspend_org_dim_val_created_amount` on `(org_id, dim, val, created_at)` with
`amount_micro` and `settled` included. Its key is exactly the key of the older
`ix_tagspend_org_dim_val_created`, so every query the older index served (the tag cap, invoice and
call-count reads, the per-dimension rollups, the team's dimension walk) is served by the new one,
and the planner stops choosing the older one once the new one is valid. Kept, it only costs disk
and an entry on every insert and update.

PostgreSQL checks first, before any change: the covering index must be present, valid and defined
as 0134 builds it, and an index of the old name must be the plain btree on those four columns.
Either check failing refuses the revision and drops nothing, so the tag reads are never left
without an index. Then DROP INDEX CONCURRENTLY IF EXISTS runs outside a transaction, like 0117.
It waits for the transactions already using the table and blocks no reads or writes. Where the
index is already gone, the revision only records itself. SQLite: plain DROP INDEX IF EXISTS.

Rollback floor: 0135 is a contract revision because it removes an index. Application queries
remain compatible; downgrade recreates the index concurrently on PostgreSQL. That scans the whole
table and can outlast the 600 s statement timeout on a large one: build it by hand first
(`CREATE INDEX CONCURRENTLY ix_tagspend_org_dim_val_created ON tagspend (org_id, dim, val,
created_at)`), and the downgrade finds it valid and only records itself.
"""
from collections.abc import Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "0135"
down_revision: str | Sequence[str] | None = "0134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True

_TABLE = "tagspend"
NAME = "ix_tagspend_org_dim_val_created"
COVERING = "ix_tagspend_org_dim_val_created_amount"
_COLUMNS = ["org_id", "dim", "val", "created_at"]
_INCLUDE = ["amount_micro", "settled"]

# The index of that name in this schema: whether it is valid, and its table, method, whether it
# is unique, partial or on expressions, how many of its columns are keys, and its columns in order.
_STATE = sa.text(
    "SELECT i.indisvalid AS valid, t.relname AS table_name, am.amname AS method, "
    "i.indisunique AS is_unique, i.indpred IS NULL AND i.indexprs IS NULL AS plain, "
    "i.indnkeyatts AS keys, "
    "array(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum ORDER BY k.ord) "
    "AS columns "
    "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_am am ON am.oid = c.relam "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = :name")


def _is(state, include: list[str]) -> bool:
    columns = list(state["columns"])
    return (state["table_name"] == _TABLE and state["method"] == "btree"
            and not state["is_unique"] and state["plain"] and state["keys"] == len(_COLUMNS)
            and columns[:state["keys"]] == _COLUMNS
            and sorted(columns[state["keys"]:]) == sorted(include))


def _checked(bind) -> bool | None:
    """The old index's state (None absent, else valid), failing before any change when the
    covering index cannot take over or the old name belongs to another index."""
    if bind.dialect.name != "postgresql":
        found = next((i for i in sa.inspect(bind).get_indexes(_TABLE) if i["name"] == NAME), None)
        if found is not None and (found["unique"] or found["column_names"] != _COLUMNS):
            raise RuntimeError(f"Refusing index migration: {NAME} is not {_TABLE}({', '.join(_COLUMNS)})")
        return None if found is None else True
    covering = bind.execute(_STATE, {"name": COVERING}).mappings().one_or_none()
    if covering is None or covering["valid"] is not True or not _is(covering, _INCLUDE):
        raise RuntimeError(
            f"Refusing index migration: {COVERING} is missing, INVALID or defined otherwise, so "
            f"dropping {NAME} would leave the tag spend reads without an index. Fix {COVERING} "
            "(see 0134) and deploy again.")
    state = bind.execute(_STATE, {"name": NAME}).mappings().one_or_none()
    if state is not None and not _is(state, []):
        raise RuntimeError(f"Refusing index migration: {NAME} is not {_TABLE}({', '.join(_COLUMNS)})")
    return None if state is None else bool(state["valid"])


@contextmanager
def _concurrent():
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default. Restore the
    # caller's actual settings even when the drop or the build times out.
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
    if _checked(bind) is None:
        return
    if bind.dialect.name != "postgresql":
        op.drop_index(NAME, table_name=_TABLE, if_exists=True)
        return
    with _concurrent():
        op.drop_index(NAME, table_name=_TABLE, postgresql_concurrently=True, if_exists=True)


def downgrade() -> None:
    bind = op.get_bind()
    state = _checked(bind)
    if bind.dialect.name != "postgresql":
        if state is None:
            op.create_index(NAME, _TABLE, _COLUMNS)
        return
    if state is True:
        return
    with _concurrent():
        if state is False:   # left INVALID by an interrupted concurrent build
            op.drop_index(NAME, table_name=_TABLE, postgresql_concurrently=True)
        op.create_index(NAME, _TABLE, _COLUMNS, postgresql_concurrently=True, if_not_exists=True)
