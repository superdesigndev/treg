"""0137 gives each existing day the spend its stored comparisons already carry."""
import asyncio
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.test_alembic_baseline import _drop_everything, _upgrade_to
from treg import audit
from treg.infra import db
from treg.maintenance import _alembic_config


@pytest.fixture
async def at_0136():
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("0136")
        yield
    finally:
        await _drop_everything()
        await db.reset_db()


async def _execute(sql: str, **params):
    async with db._engine.begin() as connection:
        return (await connection.execute(sa.text(sql), params)).all() if sql.startswith("SELECT") \
            else await connection.execute(sa.text(sql), params)


async def test_a_day_under_way_keeps_what_it_spent(at_0136):
    for ref, cost, at in (("a", 90_000, datetime(2026, 11, 26, 1)), ("b", 5_000, datetime(2026, 11, 26, 23)),
                          ("c", 70_000, datetime(2026, 11, 25, 12))):
        await _execute(
            "INSERT INTO callcomparison (org_id, user_email, call_ref, capability, a_endpoint, a_provider,"
            " b_provider, b_cost_micro, input, a_body, b_body, truncated, first, status, client, created_at)"
            " VALUES (1, 'x', :ref, 'web.search', 'tavily.web.search', 'tavily', 'exa', :cost, '', '', '',"
            " '', 'a', 'failed', '', :at)", ref=ref, cost=cost, at=at)
    for day in ("20261126", "20261124"):
        await _execute("INSERT INTO compareattemptday (day, attempts) VALUES (:day, 2)", day=day)
    await db._engine.dispose()
    await asyncio.to_thread(command.upgrade, _alembic_config(), "0137")
    rows = dict(await _execute("SELECT day, spent_micro FROM compareattemptday"))
    assert rows == {"20261126": 95_000, "20261124": 0}
