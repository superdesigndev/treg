"""0136 sets tagspend's autovacuum thresholds and downgrade resets them.

The Postgres CI job runs these against a real server.
"""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg.infra import db


migration = import_module("treg.alembic.versions.0136_tagspend_autovacuum")
# Stated here, not read from the revision, so a wrong value in the revision fails the test.
EXPECTED = {
    "autovacuum_vacuum_scale_factor": "0.01",
    "autovacuum_vacuum_insert_scale_factor": "0.01",
    "autovacuum_analyze_scale_factor": "0.02",
}


async def _apply(direction):
    def run(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            getattr(migration, direction)()

    async with db._engine.connect() as connection:
        await connection.run_sync(run)
        await connection.commit()


async def _options() -> dict[str, str]:
    async with db._engine.connect() as connection:
        options = (await connection.execute(sa.text(
            "SELECT reloptions FROM pg_class WHERE oid = 'tagspend'::regclass"))).scalar()
    return dict(option.split("=", 1) for option in options or [])


async def test_the_thresholds_are_set_and_reset(clients):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("SQLite has no autovacuum; the revision only records itself there")
    await _apply("upgrade")
    assert await _options() == EXPECTED
    await _apply("upgrade")          # a rerun sets the same values
    assert await _options() == EXPECTED
    await _apply("downgrade")
    assert await _options() == {}
    await _apply("upgrade")


async def test_sqlite_records_the_revision_only(clients):
    if db._engine.dialect.name == "postgresql":
        pytest.skip("PostgreSQL sets the thresholds")
    await _apply("upgrade")
    await _apply("downgrade")
