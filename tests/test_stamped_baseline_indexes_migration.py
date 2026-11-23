"""0116 builds the baseline indexes a database stamped at the baseline never got, plus the partial
`ix_org_stripe_default_pm`, and leaves them alone where they exist.

The cases: missing (a database stamped over a pre-Alembic schema), existing and valid (every
database Alembic created), left INVALID by an interrupted concurrent build, and a same-named index
with another definition. The Postgres CI job runs these against a real server.
"""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg.infra import db
from treg.models import Org
from tests.test_alembic_baseline import _drop_everything


migration = import_module("treg.alembic.versions.0116_stamped_baseline_indexes")
NAMES = [name for _, _, name, _ in migration._INDEXES]
PARTIAL = "ix_org_stripe_default_pm"


@pytest.fixture
async def stamped():
    """The head schema without the six indexes, as a database stamped at the baseline has it."""
    await db.reset_db()
    async with db._engine.begin() as connection:
        for name in NAMES:
            await connection.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
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


async def _indexes() -> dict[str, bool]:
    """Each present index of the six, and whether it is valid."""
    async with db._engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            rows = (await connection.execute(sa.text("""
                SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = ANY(:names)"""), {"names": NAMES})).all()
            return {name: bool(valid) for name, valid in rows}
        rows = (await connection.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type = 'index'"))).scalars().all()
        return {name: True for name in rows if name in NAMES}


async def _plan(sql: str) -> str:
    async with db._engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(sa.text("SET enable_seqscan = off"))
            return "\n".join((await connection.execute(sa.text(f"EXPLAIN {sql}"))).scalars())
        return "\n".join(str(r[-1]) for r in (await connection.execute(sa.text(
            f"EXPLAIN QUERY PLAN {sql}"))).all())


def _ddl(statements: list[str]) -> list[str]:
    return [s for s in statements if s.startswith(("CREATE INDEX", "DROP INDEX"))]


async def test_missing_indexes_are_built_and_a_rerun_changes_nothing(stamped):
    assert await _indexes() == {}
    statements: list[str] = []
    timeouts = await _apply("upgrade", statements)

    assert await _indexes() == {name: True for name in NAMES}
    assert "ix_org_stripe_customer_id" in await _plan(
        "SELECT id FROM org WHERE stripe_customer_id = 'cus_1'")
    assert PARTIAL in await _plan("SELECT id FROM org WHERE stripe_default_pm = 'pm_1'")
    ddl = _ddl(statements)
    assert len(ddl) == len(NAMES)
    if db._engine.dialect.name == "postgresql":
        assert all(s.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS") for s in ddl)
        assert "SET statement_timeout = '600s'" in statements[:statements.index(ddl[0])]
        # The timeouts are raised for the builds alone, then the caller's are back.
        assert timeouts == {"lock_timeout": "5s", "statement_timeout": "2min"}

    again: list[str] = []
    await _apply("upgrade", again)
    if db._engine.dialect.name == "postgresql":   # sqlite re-runs its own IF NOT EXISTS, a no-op
        assert not _ddl(again)
    assert await _indexes() == {name: True for name in NAMES}


async def test_existing_valid_indexes_are_left_alone():
    await db.reset_db()   # a database Alembic created: the model's indexes are all there
    assert await _indexes() == {name: True for name in NAMES}
    statements: list[str] = []
    await _apply("upgrade", statements)
    if db._engine.dialect.name == "postgresql":
        assert not [s for s in statements
                    if s.startswith(("CREATE INDEX", "DROP INDEX", "SET statement_timeout"))]
    assert await _indexes() == {name: True for name in NAMES}


async def test_downgrade_drops_only_the_new_partial_index(stamped):
    await _apply("upgrade")
    await _apply("downgrade")
    assert await _indexes() == {name: True for name in NAMES if name != PARTIAL}


@pytest.mark.parametrize("definition", [
    "CREATE INDEX ix_org_stripe_customer_id ON org (slug)",
    "CREATE UNIQUE INDEX ix_org_stripe_customer_id ON org (stripe_customer_id)",
    "CREATE INDEX ix_org_stripe_default_pm ON org (stripe_default_pm)",   # not partial
])
async def test_a_same_named_index_with_another_definition_is_refused(stamped, definition):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("the definition check reads the PostgreSQL catalog")
    async with db._engine.begin() as connection:
        await connection.execute(sa.text(definition))
    statements: list[str] = []
    with pytest.raises(RuntimeError, match="Refusing index migration"):
        await _apply("upgrade", statements)
    assert not _ddl(statements)   # refused before building anything


async def test_an_invalid_index_from_an_interrupted_build_is_dropped_and_rebuilt(stamped):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("only a PostgreSQL concurrent build leaves an INVALID index behind")
    # Real debris: a concurrent build interrupted part way leaves the index in place, INVALID. One
    # cancelled while it waits for an open write transaction on the table stops exactly there.
    async with db._engine.connect() as writer, db._engine.connect() as builder:
        await writer.execute(sa.insert(Org).values(name="a", slug="a", spent_today_micro=0))
        await builder.execution_options(isolation_level="AUTOCOMMIT")
        await builder.execute(sa.text("SET statement_timeout = '1s'"))
        with pytest.raises(sa.exc.DBAPIError):
            await builder.execute(sa.text(
                "CREATE INDEX CONCURRENTLY ix_org_stripe_customer_id ON org (stripe_customer_id)"))
        await builder.execute(sa.text("RESET statement_timeout"))
        await writer.rollback()
    assert await _indexes() == {"ix_org_stripe_customer_id": False}

    statements: list[str] = []
    await _apply("upgrade", statements)

    ddl = _ddl(statements)
    assert ddl[0].startswith("DROP INDEX CONCURRENTLY") and "ix_org_stripe_customer_id" in ddl[0]
    assert all(s.startswith("CREATE INDEX CONCURRENTLY") for s in ddl[1:])
    assert await _indexes() == {name: True for name in NAMES}
