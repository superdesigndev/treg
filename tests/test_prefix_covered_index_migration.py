"""0091 drops single-column indexes only where a surviving index leads with the same column."""
import asyncio
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic import command

from treg import audit
from treg.infra import db
from treg.maintenance import _alembic_config
from tests.test_alembic_baseline import _drop_everything, _upgrade_to


migration = import_module("treg.alembic.versions.0091_remove_prefix_covered_indexes")
DROPPED = {(table, index) for table, _, index, _ in migration._INDEXES}


@pytest.fixture
async def at_head():
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("head")
        yield
    finally:
        await _drop_everything()
        await db.reset_db()


def _names_on(connection):
    inspector = sa.inspect(connection)
    return {(table, found["name"]) for table, *_ in migration._INDEXES
            for found in [*inspector.get_indexes(table), *inspector.get_unique_constraints(table)]}


async def _names():
    async with db._engine.connect() as connection:
        return await connection.run_sync(_names_on)


async def test_drop_keeps_every_covering_index_and_downgrade_restores_the_dropped(at_head):
    after = await _names()
    assert not after & DROPPED
    assert {(table, keep) for table, _, _, keep in migration._INDEXES} <= after

    await db._engine.dispose()
    await asyncio.to_thread(command.downgrade, _alembic_config(), "0090")
    assert await _names() == after | DROPPED

    await _upgrade_to("head")
    assert await _names() == after


@pytest.mark.parametrize("survivor", [
    "ON tagspend (org_id, dim, val, created_at) WHERE val IS NOT NULL",
    "ON tagspend (dim, org_id, val, created_at)",
])
async def test_preflight_refuses_a_survivor_that_no_longer_covers_before_dropping_any_index(
        at_head, survivor):
    await db._engine.dispose()
    await asyncio.to_thread(command.downgrade, _alembic_config(), "0090")
    async with db._engine.begin() as connection:
        await connection.execute(sa.text("DROP INDEX ix_tagspend_org_dim_val_created"))
        await connection.execute(sa.text(f"CREATE INDEX ix_tagspend_org_dim_val_created {survivor}"))
    before = await _names()

    await db._engine.dispose()
    with pytest.raises(RuntimeError, match="Refusing index migration"):
        await asyncio.to_thread(command.upgrade, _alembic_config(), "0091")
    assert await _names() == before


async def test_upgrade_is_a_no_op_after_an_out_of_band_drop(at_head):
    await db._engine.dispose()
    await asyncio.to_thread(command.downgrade, _alembic_config(), "0090")
    async with db._engine.begin() as connection:
        for _, index in DROPPED:
            await connection.execute(sa.text(f"DROP INDEX IF EXISTS {index}"))
    await _upgrade_to("head")
    assert not await _names() & DROPPED
