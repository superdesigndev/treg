from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from treg import models  # noqa: F401 - populate SQLModel.metadata
from treg.config import get_settings


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit migration SQL without opening a database connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )

    async with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            # A contended deploy fails fast and clean instead of queueing the world behind an
            # ACCESS EXCLUSIVE lock, and no migration statement runs unbounded.
            await connection.execute(text("SET lock_timeout = '5s'"))
            await connection.execute(text("SET statement_timeout = '120s'"))
            await connection.commit()
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations through the async application database driver."""
    asyncio.run(_run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
