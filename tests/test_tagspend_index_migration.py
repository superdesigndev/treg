"""0134: the tag spend cap read is index-only, and a large table's index is built by hand."""
import importlib.util
from importlib.resources import files

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from httpx import AsyncClient
from sqlalchemy import text

from treg.infra.db import session_maker
from treg.models import TagSpend

_VALIDITY = text("SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                 "WHERE c.relname = :name")


def _revision():
    path = next(files("treg").joinpath("alembic", "versions").glob("0134_*.py"))
    spec = importlib.util.spec_from_file_location("revision_0134_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(module) -> None:
    def run(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            module.upgrade()
    async with session_maker.kw["bind"].connect() as connection:
        await connection.run_sync(run)
        await connection.commit()


async def _postgres_only() -> None:
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("INCLUDE and the concurrent build are PostgreSQL's")


async def test_the_index_carries_what_the_cap_read_sums(clients: AsyncClient):
    await _postgres_only()
    module = _revision()
    async with session_maker() as s:
        definition = (await s.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                                      {"n": module.NAME})).scalar_one()
    assert "(org_id, dim, val, created_at) INCLUDE (amount_micro, settled)" in definition


async def test_a_large_tagspend_is_refused_and_the_manual_build_finishes_it(clients: AsyncClient):
    await _postgres_only()
    module = _revision()
    async with session_maker() as s:
        await s.execute(text(f"DROP INDEX IF EXISTS {module.NAME}"))
        for i in range(3):
            s.add(TagSpend(org_id=1, dim="customer", val=f"c{i}", hold_id=f"h{i}", amount_micro=1))
        await s.commit()
    module.BUILD_IN_MIGRATION_MAX_ROWS = 2

    with pytest.raises(RuntimeError, match="CREATE INDEX CONCURRENTLY") as refused:
        await _run(module)
    async with session_maker() as s:
        assert (await s.execute(_VALIDITY, {"name": module.NAME})).scalar() is None

    statements = [line.strip().rstrip(";") for line in str(refused.value).splitlines()[1:]
                  if line.strip() and not line.strip().startswith("SELECT")]
    async with session_maker.kw["bind"].connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for statement in statements:
            await connection.execute(text(statement))
    await _run(module)        # a valid index: nothing to build, no refusal
    async with session_maker() as s:
        assert (await s.execute(_VALIDITY, {"name": module.NAME})).scalar() is True


async def test_a_small_tagspend_is_built_in_the_migration(clients: AsyncClient):
    await _postgres_only()
    module = _revision()
    async with session_maker() as s:
        await s.execute(text(f"DROP INDEX IF EXISTS {module.NAME}"))
        await s.commit()
    await _run(module)
    async with session_maker() as s:
        assert (await s.execute(_VALIDITY, {"name": module.NAME})).scalar() is True
