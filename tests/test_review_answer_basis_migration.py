"""0128 adds the review's code basis and answer back-off columns, `callrecord.surface` and
`idempotentcall.surface`; a rerun
after an interrupted upgrade or downgrade acts only on what is left. The Postgres CI job runs these
against a real server."""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg.infra import db
from tests.test_alembic_baseline import _drop_everything

migration = import_module("treg.alembic.versions.0128_review_answer_basis")
NAMES = {(table, name) for table, name, _ in migration.COLUMNS}


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


async def _columns() -> set[tuple[str, str]]:
    async with db._engine.connect() as connection:
        found = await connection.run_sync(lambda c: {
            (t, col["name"]) for t in ("callreview", "callrecord", "idempotentcall") for col in sa.inspect(c).get_columns(t)})
    return found & NAMES


async def _drop(*pairs):
    async with db._engine.begin() as connection:
        for table, name in pairs:
            await connection.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {name}"))


async def test_upgrade_adds_the_columns_and_downgrade_drops_them(head_schema):
    await _drop(*NAMES)
    await _apply("upgrade")
    assert await _columns() == NAMES
    await _apply("downgrade")
    assert await _columns() == set()


async def test_a_rerun_after_an_interrupted_upgrade_adds_only_what_is_missing(head_schema):
    await _drop(("callreview", "answer_attempts"), ("callrecord", "surface"))
    await _apply("upgrade")
    assert await _columns() == NAMES
