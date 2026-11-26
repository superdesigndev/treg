"""0135 drops the tagspend index 0134's covering index replaces, only once that one can take over;
refuses a same-named stranger; downgrade rebuilds it.

The Postgres CI job runs these against a real server.
"""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg.infra import db
from tests.test_alembic_baseline import _drop_everything


migration = import_module("treg.alembic.versions.0135_tagspend_drop_prefix_index")
OLD = migration.NAME
COVERING = migration.COVERING


@pytest.fixture
async def created_by_alembic():
    """The head schema plus the old index, as a database at 0134 has it."""
    await db.reset_db()
    async with db._engine.begin() as connection:
        await connection.execute(sa.text(
            f"CREATE INDEX {OLD} ON tagspend (org_id, dim, val, created_at)"))
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
                "SELECT indexname FROM pg_indexes WHERE tablename = 'tagspend'"))).scalars().all()
        else:
            rows = (await connection.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tagspend'"
            ))).scalars().all()
    return set(rows) & {OLD, COVERING}


def _ddl(statements: list[str]) -> list[str]:
    return [s for s in statements if s.startswith(("CREATE INDEX", "DROP INDEX"))]


async def test_the_old_index_is_dropped_and_a_rerun_changes_nothing(created_by_alembic):
    assert await _indexes() == {OLD, COVERING}
    statements: list[str] = []
    await _apply("upgrade", statements)
    assert await _indexes() == {COVERING}
    ddl = _ddl(statements)
    assert len(ddl) == 1
    if db._engine.dialect.name == "postgresql":
        assert ddl[0].startswith("DROP INDEX CONCURRENTLY IF EXISTS")

    again: list[str] = []
    await _apply("upgrade", again)
    assert not _ddl(again)


async def test_downgrade_rebuilds_it(created_by_alembic):
    await _apply("upgrade")
    statements: list[str] = []
    await _apply("downgrade", statements)
    assert await _indexes() == {OLD, COVERING}
    if db._engine.dialect.name == "postgresql":
        assert all(s.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS") for s in _ddl(statements))


@pytest.mark.parametrize("covering", ["missing", "without_include"])
async def test_nothing_is_dropped_while_the_covering_index_cannot_take_over(created_by_alembic, covering):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("INCLUDE is PostgreSQL's; SQLite has no tag-read index to protect")
    async with db._engine.begin() as connection:
        await connection.execute(sa.text(f"DROP INDEX {COVERING}"))
        if covering == "without_include":
            await connection.execute(sa.text(
                f"CREATE INDEX {COVERING} ON tagspend (org_id, dim, val, created_at)"))
    statements: list[str] = []
    with pytest.raises(RuntimeError, match="would leave the tag spend reads without an index"):
        await _apply("upgrade", statements)
    assert not _ddl(statements)
    assert OLD in await _indexes()


@pytest.mark.parametrize("definition", [
    f"CREATE INDEX {OLD} ON tagspend (org_id, dim, val)",
    f"CREATE INDEX {OLD} ON tagspend (org_id, dim, created_at, val)",
    f"CREATE UNIQUE INDEX {OLD} ON tagspend (org_id, dim, val, created_at)",
])
async def test_a_same_named_index_with_another_definition_is_refused(definition):
    await db.reset_db()
    try:
        async with db._engine.begin() as connection:
            await connection.execute(sa.text(definition))
        statements: list[str] = []
        with pytest.raises(RuntimeError, match="Refusing index migration"):
            await _apply("upgrade", statements)
        assert not _ddl(statements)
    finally:
        await _drop_everything()
        await db.reset_db()
