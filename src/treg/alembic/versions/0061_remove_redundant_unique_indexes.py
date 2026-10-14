"""Remove ordinary indexes already covered by single-column unique constraints.

Revision ID: 0061
Revises: 0060

The named unique constraints keep both uniqueness and indexed lookups. Only their redundant
ordinary indexes are removed; rows, foreign keys and composite-prefix indexes are untouched.
PostgreSQL checks the surviving indexes before any drop, then uses CONCURRENTLY outside a
transaction. Both directions tolerate partially completed attempts, including invalid indexes
left by an interrupted concurrent operation.

Rollback floor: 0061 is a contract revision because it removes indexes. Application queries
remain compatible; downgrade recreates the ordinary indexes concurrently on PostgreSQL before
returning to 0060. Rebuilding them requires disk space and may take longer than removing them.
"""
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None
contract = True

# table, column, redundant index, surviving constraint/index
_INDEXES = (
    ("archivekey", "key_hash", "ix_archivekey_key_hash", "uq_archive_key_hash"),
    ("oauthrefresh", "token_hash", "ix_oauthrefresh_token_hash", "uq_oauth_refresh_token"),
    ("oauthclient", "client_id", "ix_oauthclient_client_id", "uq_oauth_client_id"),
    ("oauthcode", "code", "ix_oauthcode_code", "uq_oauth_code"),
    ("arenaevaluation", "run_id", "ix_arenaevaluation_run_id", "uq_arena_evaluation"),
)

_STATE = sa.text("""
    SELECT n.nspname AS schema, old.indisvalid AS old_valid,
           keep.indisunique AND keep.indisvalid AND keep.indisready AND keep.indislive
           AND keep.indimmediate AND keep.indnatts = 1 AND keep.indnkeyatts = 1
           AND keep.indkey[0] = a.attnum AND keep.indexprs IS NULL AND keep.indpred IS NULL
           AND am.amname = 'btree'
           AND (old_class.oid IS NULL OR (
               old_class.relkind = 'i' AND old.indrelid = t.oid
               AND NOT old.indisunique AND NOT old.indisprimary AND NOT old.indisexclusion
               AND NOT old.indisreplident AND NOT old.indisclustered
               AND old.indnatts = keep.indnatts AND old.indnkeyatts = keep.indnkeyatts
               AND old_class.relam = keep_class.relam AND old.indkey = keep.indkey
               AND old.indclass = keep.indclass AND old.indcollation = keep.indcollation
               AND old.indoption = keep.indoption
               AND old.indexprs IS NULL AND old.indpred IS NULL
           )) AS safe
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attname = :column AND NOT a.attisdropped
    JOIN pg_constraint c ON c.conrelid = t.oid AND c.conname = :constraint AND c.contype = 'u'
    JOIN pg_index keep ON keep.indexrelid = c.conindid
    JOIN pg_class keep_class ON keep_class.oid = keep.indexrelid
    JOIN pg_am am ON am.oid = keep_class.relam
    LEFT JOIN pg_class old_class ON old_class.relnamespace = n.oid AND old_class.relname = :index
    LEFT JOIN pg_index old ON old.indexrelid = old_class.oid
    WHERE n.nspname = current_schema() AND t.relname = :table
""")


def _checked_indexes(bind):
    """Fail before changing anything if an installation no longer has equivalent indexes."""
    checked = []
    for table, column, index, constraint in _INDEXES:
        if bind.dialect.name == "postgresql":
            state = bind.execute(_STATE, {
                "table": table, "column": column, "index": index, "constraint": constraint,
            }).mappings().one_or_none()
            if state is None or state["safe"] is not True:
                raise RuntimeError(f"Refusing index migration: {table}.{index} / {constraint} differ")
            checked.append((table, column, index, state["schema"], state["old_valid"]))
        else:
            inspector = sa.inspect(bind)
            unique = next((c for c in inspector.get_unique_constraints(table)
                           if c["name"] == constraint), None)
            old = next((i for i in inspector.get_indexes(table) if i["name"] == index), None)
            # SQLite reflection omits expression indexes. An unreflected existing name must
            # not be mistaken for a previously removed index (or belong to a different table).
            unreflected = old is None and bind.execute(sa.text(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name"
            ), {"name": index}).scalar() is not None
            if (unique is None or unique["column_names"] != [column]
                    or unreflected
                    or (old is not None and (old["unique"] or old["column_names"] != [column]
                                             or old.get("dialect_options")))):
                raise RuntimeError(f"Refusing index migration: {table}.{index} / {constraint} differ")
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
            op.create_index(index, table, [column], schema=schema, postgresql_concurrently=True)
