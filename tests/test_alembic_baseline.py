"""Alembic head is the production schema and must match SQLModel metadata."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, text
from sqlalchemy import inspect as sa_inspect
from sqlmodel import SQLModel
from sqlmodel import select

from treg import audit
from treg.infra import db
from treg.maintenance import _alembic_config
from treg.models import Org


def _drop_present(connection) -> None:
    # Drop what is ACTUALLY in the database, not only the tables the models know: a serial run
    # reuses one sqlite file, and a table left behind by another branch or by the private admin
    # app (apikey, apikeyevent) is invisible to `metadata.drop_all` and then shows up in the
    # autogenerate diff as drift this repo cannot fix. Plain DROPs by name, not reflection: a
    # leftover may hold a foreign key to a table an earlier partial drop already removed.
    from sqlalchemy import inspect
    cascade = " CASCADE" if connection.dialect.name == "postgresql" else ""
    for name in inspect(connection).get_table_names():
        connection.execute(text(f'DROP TABLE IF EXISTS "{name}"{cascade}'))


async def _drop_everything() -> None:
    async with db._engine.begin() as connection:
        await connection.run_sync(_drop_present)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def _upgrade_to(revision: str) -> None:
    """Run the packaged migration environment without pooled async connections in its way."""
    await db._engine.dispose()
    await asyncio.to_thread(command.upgrade, _alembic_config(), revision)


def _autogenerate_diff(connection) -> list[Any]:
    # `callrecord_partitioned` (alembic 0116) is staging for a later swap, and a month partition is
    # storage created by `application.call_record_partitions`; neither is a model table.
    partitions = set()
    if connection.dialect.name == "postgresql":
        partitions = set(connection.execute(text(
            "SELECT inhrelid::regclass::text FROM pg_inherits")).scalars()) | {"callrecord_partitioned"}
    context = MigrationContext.configure(connection, opts={
        "include_name": lambda name, kind, parents: kind != "table" or name not in partitions})
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


async def _insert_at_revision(session, model, **fields) -> None:
    """Insert a row using only the columns the CURRENT schema revision has for `model`'s table."""
    name = model.__tablename__
    present = {c["name"] for c in await session.run_sync(
        lambda sync: sa_inspect(sync.connection()).get_columns(name))}
    values = {k: v for k, v in model(**fields).model_dump().items() if k in present and v is not None}
    # A table of exactly those columns: inserting through `model.__table__` would add the model's
    # defaults for every column it knows, including ones the old revision does not have yet.
    from sqlalchemy import column, table
    await session.execute(insert(table(name, *(column(k) for k in values))).values(**values))


async def _insert_org_at_revision(session, **fields) -> int:
    """Insert an org row using only the columns the CURRENT schema revision has."""
    await _insert_at_revision(session, Org, **fields)
    return (await session.execute(select(Org.id).where(Org.slug == fields["slug"]))).scalar_one()
