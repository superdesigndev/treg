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


def _session_timeouts() -> tuple[str, ...]:
    lock_ms = round(get_settings().migration_lock_timeout_s * 1000)
    return (f"SET lock_timeout = '{lock_ms}ms'", "SET statement_timeout = '120s'")


def _restore_session_timeouts(ctx, step, heads, run_args) -> None:
    # An index revision raises these for its CONCURRENTLY build and restores fixed values; the
    # next revision in the same run must still wait no longer than the setting.
    if ctx.connection.dialect.name == "postgresql":
        for statement in _session_timeouts():
            ctx.connection.execute(text(statement))


def _run_migrations(connection) -> None:
    # Each revision commits on its own, so a lock timeout in one leaves every earlier revision
    # applied and stamped; `maintenance` retries the upgrade and it resumes where it stopped.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
        on_version_apply=_restore_session_timeouts,
    )

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
            for statement in _session_timeouts():
                await connection.execute(text(statement))
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
