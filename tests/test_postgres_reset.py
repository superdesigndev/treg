"""Test isolation must clear data without rebuilding the whole schema, and rebuild it when a test
changed it."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, inspect, select, text
from sqlmodel import SQLModel

from treg.infra.db import _engine, reset_db


@pytest.mark.skipif(
    not os.environ.get("TREG_TEST_DB_URL"), reason="requires the Postgres test database",
)
async def test_repeated_reset_keeps_postgres_table_identity() -> None:
    """A test boundary clears rows and sequences, not tables, constraints, and indexes."""
    await reset_db()
    async with _engine.connect() as connection:
        before = await connection.scalar(text("SELECT 'callrecord'::regclass::oid"))

    await reset_db()
    async with _engine.connect() as connection:
        after = await connection.scalar(text("SELECT 'callrecord'::regclass::oid"))

    assert after == before, "rebuilding the schema per test floods Postgres with avoidable DDL and WAL"


async def test_a_test_that_alters_the_schema_gets_it_rebuilt_by_the_next_reset() -> None:
    """Resets skip the schema check while no DDL ran; a test's own DDL makes the next one look."""
    await reset_db()
    async with _engine.begin() as connection:
        await connection.execute(text("CREATE INDEX ix_reset_probe ON callrecord (endpoint_id)"))

    await reset_db()
    async with _engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: {index["name"] for index in inspect(sync).get_indexes("callrecord")})

    assert "ix_reset_probe" not in names


async def test_a_reset_empties_what_a_test_wrote_and_restarts_ids() -> None:
    """Only tables holding rows are emptied, and every id restarts at 1, including a table the
    test emptied itself after advancing its sequence."""
    misses, snapshots = SQLModel.metadata.tables["searchmiss"], SQLModel.metadata.tables["capacitysnapshot"]
    await reset_db()
    async with _engine.begin() as connection:
        await connection.execute(misses.insert(), [{"query": "a"}, {"query": "b"}])
        await connection.execute(snapshots.insert().values(provider="p"))
        await connection.execute(snapshots.delete())

    await reset_db()
    async with _engine.begin() as connection:
        assert await connection.scalar(select(func.count()).select_from(misses)) == 0
        await connection.execute(misses.insert().values(query="c"))
        await connection.execute(snapshots.insert().values(provider="q"))
        assert await connection.scalar(select(misses.c.id)) == 1
        assert await connection.scalar(select(snapshots.c.id)) == 1
