"""Alembic head is the production schema and must match SQLModel metadata."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlmodel import SQLModel

from treg import audit
from treg.infra import db
from treg.maintenance import _alembic_config


async def _drop_everything() -> None:
    async with db._engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def _upgrade_to(revision: str) -> None:
    """Run the packaged migration environment without pooled async connections in its way."""
    await db._engine.dispose()
    await asyncio.to_thread(command.upgrade, _alembic_config(), revision)


def _autogenerate_diff(connection) -> list[Any]:
    context = MigrationContext.configure(connection)
    return compare_metadata(context, SQLModel.metadata)


async def test_alembic_head_has_no_model_drift():
    """Alembic is authoritative, so head must match SQLModel metadata exactly."""
    await audit.drain()
    await _drop_everything()

    try:
        await _upgrade_to("head")
        async with db._engine.connect() as connection:
            diff = await connection.run_sync(_autogenerate_diff)
        assert diff == []
    finally:
        await _drop_everything()
        await db.reset_db()
