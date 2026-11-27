"""Test isolation must clear data without rebuilding the whole schema, and rebuild it when a test
changed it."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text

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
