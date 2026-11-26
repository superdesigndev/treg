"""0127 adds `served_call_ref` and `served_content_hash` to `callrecord`; a rerun after an
interrupted upgrade or downgrade acts only on what is left, and `--sql` renders both directions
without a database.

The Postgres CI job runs these against a real server.
"""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg.infra import db
from tests.test_alembic_baseline import _drop_everything

migration = import_module("treg.alembic.versions.0127_callrecord_served_call_ref")


@pytest.fixture
async def head_schema():
    await db.reset_db()
    try:
        yield
    finally:
        await _drop_everything()
        await db.reset_db()


async def _apply(direction):
    def run(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            getattr(migration, direction)()

    async with db._engine.connect() as connection:
        await connection.run_sync(run)
        await connection.commit()


async def _columns() -> set[str]:
    async with db._engine.connect() as connection:
        names = await connection.run_sync(
            lambda c: {col["name"] for col in sa.inspect(c).get_columns("callrecord")})
    return names & set(migration.COLUMNS)


async def _drop(*names):
    async with db._engine.begin() as connection:
        for name in names:
            await connection.execute(sa.text(f"ALTER TABLE callrecord DROP COLUMN {name}"))


async def test_upgrade_adds_both_columns_and_downgrade_drops_them(head_schema):
    await _drop(*migration.COLUMNS)
    await _apply("upgrade")
    assert await _columns() == set(migration.COLUMNS)
    await _apply("downgrade")
    assert await _columns() == set()


async def test_a_rerun_after_an_interrupted_upgrade_adds_the_missing_column(head_schema):
    await _drop("served_content_hash")   # interrupted after the first ADD COLUMN, version 0126
    assert await _columns() == {"served_call_ref"}
    await _apply("upgrade")
    assert await _columns() == set(migration.COLUMNS)
    await _apply("upgrade")   # and once both exist, a further rerun changes nothing
    assert await _columns() == set(migration.COLUMNS)


async def test_a_rerun_after_an_interrupted_downgrade_drops_the_rest(head_schema):
    await _drop("served_content_hash")   # interrupted after the first DROP COLUMN, version 0127
    await _apply("downgrade")
    assert await _columns() == set()
    async with db._engine.connect() as connection:
        tables = await connection.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
    assert "_alembic_tmp_callrecord" not in tables, "no table rebuild to leave half done"


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
@pytest.mark.parametrize("direction, verb", [("upgrade", "ADD COLUMN"), ("downgrade", "DROP COLUMN")])
def test_offline_sql_renders_both_columns(dialect, direction, verb):
    """`alembic upgrade 0126:0127 --sql` has no database to inspect: it renders the DDL as is."""
    import io

    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(migration, direction)()
    sql = buffer.getvalue()
    for name in migration.COLUMNS:
        assert f"{verb} {name}" in sql, sql
