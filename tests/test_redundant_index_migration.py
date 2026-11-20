"""Concurrent index removal preserves lookups, uniqueness and retryable schema changes."""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

from treg.infra import db
from tests.test_alembic_baseline import _drop_everything


migration = import_module("treg.alembic.versions.0061_remove_redundant_unique_indexes")
CASES = (
    ("archivekey", "key_hash", "uq_archive_key_hash"),
    ("oauthrefresh", "token_hash", "uq_oauth_refresh_token"),
    ("oauthclient", "client_id", "uq_oauth_client_id"),
    ("oauthcode", "code", "uq_oauth_code"),
    ("arenaevaluation", "run_id", "uq_arena_evaluation"),
)


@pytest.fixture
async def index_schema(request):
    """Small populated tables exercise real DDL on SQLite and PostgreSQL CI alike."""
    await _drop_everything()
    metadata = sa.MetaData()
    damage = getattr(request, "param", None)
    for name, column, constraint in CASES:
        # Damage the last table to verify the preflight completes before any index is removed.
        damaged = name == "arenaevaluation"
        table = sa.Table(name, metadata, sa.Column("id", sa.Integer, primary_key=True),
                         sa.Column(column, sa.String, nullable=False), sa.Column("other", sa.String))
        if not (damaged and damage == "missing_unique"):
            table.append_constraint(sa.UniqueConstraint(column, name=constraint))
        key = "other" if damaged and damage == "wrong_column" else column
        where = {"sqlite_where": table.c.id > 0, "postgresql_where": table.c.id > 0}
        expression = sa.func.lower(table.c[key]) if damaged and damage == "expression" else table.c[key]
        sa.Index(f"ix_{name}_{column}", expression,
                 **(where if damaged and damage == "partial" else {}))
        sa.Index(f"ix_{name}_other", table.c.other)
    async with db._engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        for name, column, _ in CASES:
            table = metadata.tables[name]
            await connection.execute(table.insert().values(
                id=1, **{column: "existing"}, other="kept"))
    try:
        yield metadata
    finally:
        await _drop_everything()
        await db.reset_db()


async def _apply(direction, statements=None):
    def run(connection):
        def record(conn, cursor, statement, parameters, context, executemany):
            if statements is not None:
                statements.append(statement)

        sa.event.listen(connection, "before_cursor_execute", record)
        try:
            context = MigrationContext.configure(connection)
            with context.begin_transaction(), Operations.context(context):
                getattr(migration, direction)()
        finally:
            sa.event.remove(connection, "before_cursor_execute", record)

    async with db._engine.connect() as connection:
        await connection.run_sync(run)
        await connection.commit()


async def _index_names():
    async with db._engine.connect() as connection:
        return await connection.run_sync(_index_names_on)


def _index_names_on(connection):
    if connection.dialect.name == "sqlite":
        # PRAGMA index_list can see an older schema on a pooled connection after another one
        # performed DDL. Read the catalog itself; this also keeps expression indexes in the
        # before/after assertion instead of letting reflection silently omit them.
        query = sa.text("""
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND tbl_name IN :tables
              AND name NOT GLOB 'sqlite_autoindex_*'
        """).bindparams(sa.bindparam("tables", expanding=True))
        return set(connection.execute(query, {"tables": [table for table, _, _ in CASES]}).scalars())
    return {
        index["name"] for table, _, _ in CASES for index in sa.inspect(connection).get_indexes(table)
    }


async def test_sqlite_index_snapshot_sees_ddl_from_another_connection(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'indexes.db'}")
    try:
        async with engine.connect() as reader:
            # Warm this connection's schema before a different connection performs the DDL.
            assert await reader.run_sync(lambda conn: sa.inspect(conn).get_table_names()) == []
            await reader.commit()
            async with engine.begin() as writer:
                for table, column, constraint in CASES:
                    await writer.execute(sa.text(
                        f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, "{column}" TEXT, '
                        f'other TEXT, CONSTRAINT "{constraint}" UNIQUE ("{column}"))'))
                    await writer.execute(sa.text(
                        f'CREATE INDEX "ix_{table}_{column}" ON "{table}" ("{column}")'))
                    await writer.execute(sa.text(
                        f'CREATE INDEX "ix_{table}_other" ON "{table}" (other)'))
                await writer.execute(sa.text(
                    'CREATE INDEX ix_archivekey_expression ON archivekey (lower(key_hash))'))
            expected = {f"ix_{table}_{suffix}" for table, column, _ in CASES
                        for suffix in (column, "other")} | {"ix_archivekey_expression"}
            assert await reader.run_sync(_index_names_on) == expected
            assert await reader.run_sync(_index_names_on) == expected
    finally:
        await engine.dispose()


async def _assert_data_and_uniqueness(metadata):
    async with db._engine.begin() as connection:
        for name, column, _ in CASES:
            table = metadata.tables[name]
            assert (await connection.execute(sa.select(table.c.other).where(
                table.c[column] == "existing"))).scalar_one() == "kept"
            with pytest.raises(sa.exc.IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(table.insert().values(
                        id=2, **{column: "existing"}, other="duplicate"))


async def test_index_removal_preserves_constraints_data_and_supports_downgrade(index_schema):
    before = await _index_names()
    statements = []
    await _apply("upgrade", statements)
    await _apply("upgrade")  # a previous attempt can finish DDL but fail before the version stamp
    removed = {f"ix_{table}_{column}" for table, column, _ in CASES}
    assert await _index_names() == before - removed
    await _assert_data_and_uniqueness(index_schema)

    await _apply("downgrade", statements)
    await _apply("downgrade")
    assert await _index_names() == before
    await _assert_data_and_uniqueness(index_schema)
    if db._engine.dialect.name == "postgresql":
        ddl = [s.strip() for s in statements
               if s.strip().startswith(("DROP INDEX", "CREATE INDEX"))]
        assert len(ddl) == 10
        assert all("INDEX CONCURRENTLY" in s for s in ddl)


async def test_index_removal_resumes_after_a_partial_attempt(index_schema):
    async with db._engine.begin() as connection:
        await connection.execute(sa.text("DROP INDEX ix_oauthclient_client_id"))
    await _apply("upgrade")
    assert not (await _index_names()) & {f"ix_{t}_{c}" for t, c, _ in CASES}
    await _assert_data_and_uniqueness(index_schema)


@pytest.mark.parametrize("index_schema", ["missing_unique", "wrong_column", "partial", "expression"],
                         indirect=True)
async def test_preflight_refuses_drift_before_dropping_any_index(index_schema):
    before = await _index_names()
    with pytest.raises(RuntimeError, match="Refusing index migration"):
        await _apply("upgrade")
    assert await _index_names() == before


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_postgres_interrupted_drop_is_retryable_and_restores_timeouts(index_schema, direction):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent DDL and transaction waits")

    # A transaction that has used the index prevents DROP INDEX CONCURRENTLY from completing.
    # Cancel after it marks the ordinary index invalid, then retry against that real debris.
    async with db._engine.connect() as reader:
        await reader.execute(sa.text("SELECT * FROM archivekey WHERE key_hash = 'existing'"))
        async with db._engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(sa.text("SET lock_timeout = '100ms'"))
            try:
                with pytest.raises(sa.exc.DBAPIError, match="lock timeout"):
                    await connection.execute(sa.text(
                        "DROP INDEX CONCURRENTLY ix_archivekey_key_hash"))
            finally:
                await connection.execute(sa.text("RESET lock_timeout"))
        await reader.rollback()

    async with db._engine.connect() as connection:
        assert (await connection.execute(sa.text("""
            SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_archivekey_key_hash'::regclass
        """))).scalar_one() is False
        await connection.execute(sa.text("SET lock_timeout = '7s'"))
        await connection.execute(sa.text("SET statement_timeout = '13s'"))
        await connection.commit()

        def run(conn):
            context = MigrationContext.configure(conn)
            with context.begin_transaction(), Operations.context(context):
                getattr(migration, direction)()
            assert conn.execute(sa.text("SHOW lock_timeout")).scalar_one() == "7s"
            assert conn.execute(sa.text("SHOW statement_timeout")).scalar_one() == "13s"

        try:
            await connection.run_sync(run)
        finally:
            await connection.execute(sa.text("RESET lock_timeout"))
            await connection.execute(sa.text("RESET statement_timeout"))
            await connection.commit()
    await _assert_data_and_uniqueness(index_schema)
    async with db._engine.connect() as connection:
        invalid = (await connection.execute(sa.text("""
            SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND NOT i.indisvalid
        """))).scalar_one()
        assert invalid == 0
