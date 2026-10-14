"""Postgres test isolation must clear data without rebuilding the whole schema."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

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
