"""0102 lets callrecord's id sequence and every cursor that stores a callrecord id pass 2,147,483,647.

On Postgres an `integer` column or sequence fails there with "out of range"; the Postgres CI job
runs these against a real server. SQLite stores 64-bit integers either way, so there they check
that the upgrade keeps every stored cursor.
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from treg.infra import db
from tests.test_alembic_baseline import _drop_everything, _upgrade_to

INT4_MAX = 2_147_483_647
CURSORS = {
    "endpointstatcursor": "cursor_id",
    "callusagecursor": "cursor_id",
    "shadowcursor": "cursor_id",
    "webarenacallcursor": "call_id",
}


@pytest.fixture
async def at_0101():
    await _drop_everything()
    await _upgrade_to("0101")
    try:
        yield
    finally:
        await _drop_everything()
        await db.reset_db()


async def _execute(sql: str, **params):
    async with db._engine.begin() as connection:
        result = await connection.execute(sa.text(sql), params)
        return result.all() if result.returns_rows else []


async def _seed(cursor: int) -> None:
    for table, column in CURSORS.items():
        extra = ", history_from" if table == "callusagecursor" else ""
        value = ", '2026-01-01'" if extra else ""
        await _execute(f"INSERT INTO {table} (id, {column}, updated_at{extra}) "
                       f"VALUES ('callrecord', :v, CURRENT_TIMESTAMP{value})", v=cursor)


async def test_the_upgrade_keeps_every_cursor_and_lifts_the_integer_cap(at_0101):
    await _seed(INT4_MAX)
    await _upgrade_to("0102")
    for table, column in CURSORS.items():
        assert await _execute(f"SELECT {column} FROM {table}") == [(INT4_MAX,)]
        await _execute(f"UPDATE {table} SET {column} = :v", v=INT4_MAX + 1)
        assert await _execute(f"SELECT {column} FROM {table}") == [(INT4_MAX + 1,)]


async def test_the_callrecord_sequence_can_hand_out_ids_past_the_integer_cap(at_0101):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("SQLite has no sequences")
    sequence = "pg_get_serial_sequence('callrecord', 'id')"
    await _upgrade_to("0102")
    assert await _execute(
        f"SELECT format_type(seqtypid, NULL), seqmax FROM pg_sequence WHERE seqrelid = {sequence}::regclass"
    ) == [("bigint", 2**63 - 1)]
    await _execute(f"SELECT setval({sequence}, :v)", v=INT4_MAX)
    assert await _execute(f"SELECT nextval({sequence})") == [(INT4_MAX + 1,)]


async def test_an_upgrade_rerun_after_a_partial_run_finishes_the_rest(at_0101):
    if db._engine.dialect.name != "postgresql":
        pytest.skip("SQLite rewrites every table in one transaction")
    await _execute("ALTER TABLE endpointstatcursor ALTER COLUMN cursor_id TYPE BIGINT")
    await _seed(1)
    await _upgrade_to("0102")
    types = dict(await _execute(
        "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND column_name IN ('cursor_id', 'call_id', 'cursor', "
        "'first_id', 'highwater_id', 'last_id') AND table_name IN ('endpointstatcursor', "
        "'callusagecursor', 'shadowcursor', 'shadowpair', 'arenainsightstate', 'webarenacallcursor', "
        "'webarenaseedprogress')"))
    assert len(types) == 9 and set(types.values()) == {"bigint"}
