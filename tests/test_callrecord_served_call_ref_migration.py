"""0127 adds `served_call_ref` and `served_content_hash` to `callrecord`, and a rerun after an
interrupted upgrade (the first column added, the version still 0126) adds only what is missing.

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
