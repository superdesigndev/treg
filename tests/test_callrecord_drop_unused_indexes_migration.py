"""0117 drops six unused single-column `callrecord` indexes where they exist, records itself where
they never did, refuses a same-named stranger, and downgrade rebuilds them.

The Postgres CI job runs these against a real server.
"""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg.infra import db
from tests.test_alembic_baseline import _drop_everything


migration = import_module("treg.alembic.versions.0117_callrecord_drop_unused_indexes")
NAMES = [name for _, name in migration._INDEXES]


@pytest.fixture
async def created_by_alembic():
    """The head schema plus the six indexes, as a database Alembic created before 0117 has it."""
    await db.reset_db()
    async with db._engine.begin() as connection:
        for column, name in migration._INDEXES:
            await connection.execute(sa.text(f"CREATE INDEX {name} ON callrecord ({column})"))
    try:
        yield
    finally:
        await _drop_everything()
        await db.reset_db()


async def _apply(direction, statements=None):
    def run(connection):
        def record(conn, cursor, statement, *args):
            if statements is not None:
                statements.append(statement.strip())
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


async def _indexes() -> set[str]:
    async with db._engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            rows = (await connection.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'callrecord'"))).scalars().all()
        else:
            rows = (await connection.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'callrecord'"
            ))).scalars().all()
    return set(rows) & set(NAMES)


def _ddl(statements: list[str]) -> list[str]:
    return [s for s in statements if s.startswith(("CREATE INDEX", "DROP INDEX"))]


async def test_present_indexes_are_dropped_and_a_rerun_changes_nothing(created_by_alembic):
    assert await _indexes() == set(NAMES)
    statements: list[str] = []
    await _apply("upgrade", statements)

    assert await _indexes() == set()
    ddl = _ddl(statements)
    assert len(ddl) == len(NAMES)
    if db._engine.dialect.name == "postgresql":
        assert all(s.startswith("DROP INDEX CONCURRENTLY IF EXISTS") for s in ddl)

    again: list[str] = []
    await _apply("upgrade", again)
    assert not _ddl(again)


async def test_a_database_that_never_had_them_only_records_the_revision():
    await db.reset_db()   # the head model declares none of the six, like a stamped database
    assert await _indexes() == set()
    statements: list[str] = []
    await _apply("upgrade", statements)
    assert not _ddl(statements)
    assert not [s for s in statements if s.startswith("SET statement_timeout")]


async def test_downgrade_rebuilds_them(created_by_alembic):
    await _apply("upgrade")
    statements: list[str] = []
    await _apply("downgrade", statements)
    assert await _indexes() == set(NAMES)
    if db._engine.dialect.name == "postgresql":
        assert all(s.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS") for s in _ddl(statements))


@pytest.mark.parametrize("definition", [
    "CREATE INDEX ix_callrecord_provider ON callrecord (endpoint_id)",
    "CREATE INDEX ix_callrecord_provider ON callrecord (provider, created_at)",
    "CREATE UNIQUE INDEX ix_callrecord_provider ON callrecord (provider)",
])
async def test_a_same_named_index_with_another_definition_is_refused(definition):
    await db.reset_db()
    try:
        async with db._engine.begin() as connection:
            await connection.execute(sa.text(definition))
        statements: list[str] = []
        with pytest.raises(RuntimeError, match="Refusing index migration"):
            await _apply("upgrade", statements)
        assert not _ddl(statements)   # refused before dropping anything
    finally:
        await _drop_everything()
        await db.reset_db()
