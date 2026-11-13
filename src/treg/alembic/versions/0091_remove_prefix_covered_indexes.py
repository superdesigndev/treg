"""Remove single-column indexes already served by the leading column of a composite index.

Revision ID: 0091
Revises: 0090

Each dropped index holds exactly the first column of a surviving composite index or unique
constraint on the same table, so every lookup, foreign-key check and range it served is served by
the survivor; the dropped one only costs disk and write amplification. Rows, foreign keys and the
survivors are untouched.

PostgreSQL checks every survivor before any drop, then uses CONCURRENTLY outside a transaction,
as 0061 does. The drops are cheap, but on a busy table each waits for the transactions already
using the index; on such a database run the `DROP INDEX CONCURRENTLY IF EXISTS` statements out of
band before deploying, and this revision finds them gone and only records the revision. Both
directions tolerate partially completed attempts, including invalid indexes left by an interrupted
concurrent operation.

Rollback floor: 0091 is a contract revision because it removes indexes. Application queries remain
compatible; downgrade recreates the indexes concurrently on PostgreSQL before returning to 0090.
Rebuilding them scans each table and needs disk space, so it takes far longer than removing them.
"""
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None
contract = True

# table, column, redundant index, surviving index or unique constraint that leads with `column`
_INDEXES = (
    ("ledgerentry", "org_id", "ix_ledgerentry_org_id", "ix_ledgerentry_org_id_created_at"),
    ("archivesnapshot", "key_id", "ix_archivesnapshot_key_id", "uq_archive_snapshot_version"),
    ("callrecord", "org_id", "ix_callrecord_org_id", "ix_callrecord_org_id_created_at"),
    ("archivekeyorg", "org_id", "ix_archivekeyorg_org_id", "uq_archive_key_org"),
    ("tagspend", "org_id", "ix_tagspend_org_id", "ix_tagspend_org_dim_val_created"),
)

# A unique constraint's backing index carries the constraint's name, so one lookup by relname
# finds either kind of survivor.
_STATE = sa.text("""
    SELECT n.nspname AS schema, old.indisvalid AS old_valid,
           keep.indisvalid AND keep.indisready AND keep.indislive
           AND keep.indkey[0] = a.attnum AND keep.indexprs IS NULL AND keep.indpred IS NULL
           AND am.amname = 'btree'
           AND (old_class.oid IS NULL OR (
               old_class.relkind = 'i' AND old.indrelid = t.oid
               AND NOT old.indisunique AND NOT old.indisprimary AND NOT old.indisexclusion
               AND NOT old.indisreplident AND NOT old.indisclustered
               AND old.indnatts = 1 AND old.indkey[0] = a.attnum
               AND old_class.relam = keep_class.relam
               AND old.indexprs IS NULL AND old.indpred IS NULL
           )) AS safe
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attname = :column AND NOT a.attisdropped
    JOIN pg_class keep_class ON keep_class.relnamespace = n.oid AND keep_class.relname = :keep
    JOIN pg_index keep ON keep.indexrelid = keep_class.oid AND keep.indrelid = t.oid
    JOIN pg_am am ON am.oid = keep_class.relam
    LEFT JOIN pg_class old_class ON old_class.relnamespace = n.oid AND old_class.relname = :index
    LEFT JOIN pg_index old ON old.indexrelid = old_class.oid
    WHERE n.nspname = current_schema() AND t.relname = :table
""")


def _sqlite_leading(bind, table: str, name: str) -> str | None:
    inspector = sa.inspect(bind)
    for found in [*inspector.get_indexes(table), *inspector.get_unique_constraints(table)]:
        # A partial survivor (`sqlite_where`) does not serve every lookup on its leading column.
        if found["name"] == name and found["column_names"] and not found.get("dialect_options"):
            return found["column_names"][0]
    return None


def _checked_indexes(bind):
    """Fail before changing anything if an installation no longer has a covering index."""
    checked = []
    for table, column, index, keep in _INDEXES:
        if bind.dialect.name == "postgresql":
            state = bind.execute(_STATE, {
                "table": table, "column": column, "index": index, "keep": keep,
            }).mappings().one_or_none()
            if state is None or state["safe"] is not True:
                raise RuntimeError(f"Refusing index migration: {table}.{index} / {keep} differ")
            checked.append((table, column, index, state["schema"], state["old_valid"]))
        else:
            old = next((i for i in sa.inspect(bind).get_indexes(table) if i["name"] == index), None)
            # SQLite reflection omits expression indexes. An unreflected existing name must
            # not be mistaken for a previously removed index.
            unreflected = old is None and bind.execute(sa.text(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name"
            ), {"name": index}).scalar() is not None
            if (_sqlite_leading(bind, table, keep) != column or unreflected
                    or (old is not None and (old["unique"] or old["column_names"] != [column]
                                             or old.get("dialect_options")))):
                raise RuntimeError(f"Refusing index migration: {table}.{index} / {keep} differ")
            checked.append((table, column, index, None, True if old is not None else None))
    return checked


@contextmanager
def _concurrent():
    # Like the earlier concurrent builds, this can wait for maintenance without blocking DML.
    # Restore the caller's actual settings even when a drop or build times out.
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
    if op.get_bind().dialect.name != "postgresql":
        for table, _, index, _, _ in _checked_indexes(op.get_bind()):
            op.drop_index(index, table_name=table, if_exists=True)
        return
    with _concurrent() as bind:
        for table, _, index, schema, _ in _checked_indexes(bind):
            op.drop_index(index, table_name=table, schema=schema,
                          postgresql_concurrently=True, if_exists=True)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        for table, column, index, _, valid in _checked_indexes(op.get_bind()):
            if valid is None:
                op.create_index(index, table, [column])
        return
    with _concurrent() as bind:
        for table, column, index, schema, valid in _checked_indexes(bind):
            if valid is True:
                continue
            if valid is False:
                op.drop_index(index, table_name=table, schema=schema, postgresql_concurrently=True)
            op.create_index(index, table, [column], schema=schema, postgresql_concurrently=True,
                            if_not_exists=True)
