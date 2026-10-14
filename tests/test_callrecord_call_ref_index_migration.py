"""0072 builds `ix_callrecord_call_ref` where a database never got it, and leaves it alone where it has.

The cases: missing (a database stamped at the baseline over a pre-Alembic `callrecord`), existing
and valid (every database Alembic created), and left INVALID by an interrupted concurrent build.
On PostgreSQL the build is concurrent, runs without the migration statement timeout, and restores
the caller's timeouts after; the Postgres CI job runs these against a real server.
"""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg.infra import db
from tests.test_alembic_baseline import _drop_everything


migration = import_module("treg.alembic.versions.0072_callrecord_call_ref_index")
NAME = "ix_callrecord_call_ref"


@pytest.fixture
async def callrecord():
    """A bare `callrecord` with duplicate refs, as the old `init_db` left it: no call_ref index."""
    await _drop_everything()
    async with db._engine.begin() as connection:
        await connection.execute(sa.text(
            "CREATE TABLE callrecord (id INTEGER PRIMARY KEY, call_ref VARCHAR NOT NULL, other VARCHAR)"))
        await connection.execute(sa.text(
            "INSERT INTO callrecord (id, call_ref, other) VALUES (1, 'a', 'x'), (2, 'a', 'y'), (3, 'b', 'z')"))
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
            if connection.dialect.name == "postgresql":
                return {name: connection.execute(sa.text(f"SHOW {name}")).scalar_one()
                        for name in ("lock_timeout", "statement_timeout")}
            return {}
        finally:
            sa.event.remove(connection, "before_cursor_execute", record)

    async with db._engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(sa.text("SET lock_timeout = '5s'"))
            await connection.execute(sa.text("SET statement_timeout = '120s'"))   # what env.py sets
            await connection.commit()
        try:
            return await connection.run_sync(run)
        finally:
            if connection.dialect.name == "postgresql":
                await connection.execute(sa.text("RESET lock_timeout"))
                await connection.execute(sa.text("RESET statement_timeout"))
            await connection.commit()


async def _index():
    """`None` (absent) or whether the index is valid, plus its columns."""
    async with db._engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            row = (await connection.execute(sa.text("""
                SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = :name"""), {"name": NAME})).first()
            return None if row is None else bool(row[0])
        found = (await connection.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"), {"name": NAME})).first()
        return None if found is None else True


async def _uses_index() -> bool:
    async with db._engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(sa.text("SET enable_seqscan = off"))
            plan = "\n".join((await connection.execute(sa.text(
                "EXPLAIN SELECT id FROM callrecord WHERE call_ref IN ('a', 'b')"))).scalars())
        else:
            plan = "\n".join(str(r[-1]) for r in (await connection.execute(sa.text(
                "EXPLAIN QUERY PLAN SELECT id FROM callrecord WHERE call_ref IN ('a', 'b')"))).all())
        return NAME in plan


async def test_a_missing_index_is_built_and_a_rerun_changes_nothing(callrecord):
    assert await _index() is None
    statements: list[str] = []
    timeouts = await _apply("upgrade", statements)

    assert await _index() is True
    assert await _uses_index()
    ddl = [s for s in statements if s.startswith(("CREATE INDEX", "DROP INDEX"))]
    assert len(ddl) == 1
    if db._engine.dialect.name == "postgresql":
        assert ddl[0].startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS")
        # The statement timeout is lifted for the build alone, then the caller's is back.
        build = statements.index(ddl[0])
        assert "SET statement_timeout = 0" in statements[:build]
        assert timeouts == {"lock_timeout": "5s", "statement_timeout": "2min"}

    again: list[str] = []
    await _apply("upgrade", again)
    if db._engine.dialect.name == "postgresql":   # sqlite re-runs its own IF NOT EXISTS, a no-op
        assert not [s for s in again if s.startswith(("CREATE INDEX", "DROP INDEX"))]
    assert await _index() is True


async def test_an_existing_valid_index_is_left_alone(callrecord):
    async with db._engine.begin() as connection:
        await connection.execute(sa.text(f"CREATE INDEX {NAME} ON callrecord (call_ref)"))
    statements: list[str] = []
    await _apply("upgrade", statements)
    if db._engine.dialect.name == "postgresql":
        assert not [s for s in statements if s.startswith(("CREATE INDEX", "DROP INDEX", "SET statement_timeout"))]
    assert await _index() is True


async def test_downgrade_keeps_the_index_that_0001_owns(callrecord):
    await _apply("upgrade")
    await _apply("downgrade")
    assert await _index() is True


async def test_a_same_named_index_on_something_else_is_refused(callrecord):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("the definition check reads the PostgreSQL catalog")
    async with db._engine.begin() as connection:
        await connection.execute(sa.text(f"CREATE INDEX {NAME} ON callrecord (other)"))
    with pytest.raises(RuntimeError, match="Refusing index migration"):
        await _apply("upgrade")


async def test_an_invalid_index_from_an_interrupted_build_is_dropped_and_rebuilt(callrecord):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("only a PostgreSQL concurrent build leaves an INVALID index behind")
    # Real debris: a concurrent build that fails part way leaves the index in place, INVALID.
    # A unique build over the duplicate refs fails exactly like that.
    async with db._engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        with pytest.raises(sa.exc.DBAPIError):
            await connection.execute(sa.text(f"CREATE UNIQUE INDEX CONCURRENTLY {NAME} ON callrecord (call_ref)"))
    assert await _index() is False

    statements: list[str] = []
    await _apply("upgrade", statements)

    ddl = [s for s in statements if s.startswith(("CREATE INDEX", "DROP INDEX"))]
    assert [s.split(" IF")[0].split(" ix_")[0] for s in ddl] == ["DROP INDEX CONCURRENTLY", "CREATE INDEX CONCURRENTLY"]
    assert await _index() is True
    assert await _uses_index()
