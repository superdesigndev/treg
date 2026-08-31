"""Explicit, ordered maintenance tasks run once per release before serving."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from importlib.resources import files

from alembic import command, util
from alembic.config import Config
from sqlalchemy import inspect

from .application.connect import _backfill_provider_extra_tools
from .infra.db import _db_url, _engine


ReleaseTask = tuple[str, Callable[[], Awaitable[int]]]

# Content-driven tasks stay in a stable order so every release applies the same repairs before
# new code serves traffic. Task bodies remain with their owning subsystem; this is orchestration.
RELEASE_TASKS: tuple[ReleaseTask, ...] = (
    ("provider companion tools", _backfill_provider_extra_tools),
)


def _alembic_config() -> Config:
    """The one Config both the deploy path and the tests build: the packaged script directory,
    pointed at the same URL the engine uses (%-escaped for configparser interpolation)."""
    config = Config()
    config.set_main_option("script_location", str(files("treg").joinpath("alembic")))
    config.set_main_option("sqlalchemy.url", _db_url.replace("%", "%%"))
    return config


async def _table_names() -> set[str]:
    async with _engine.connect() as connection:
        return await connection.run_sync(lambda sync_connection: set(
            inspect(sync_connection).get_table_names()
        ))


async def _upgrade_schema() -> None:
    tables = await _table_names()
    config = _alembic_config()

    if not tables or "alembic_version" in tables:
        state = "empty" if not tables else "stamped"
        try:
            await asyncio.to_thread(command.upgrade, config, "head")
        except util.CommandError as exc:
            if "Can't locate revision" not in str(exc):
                raise
            raise RuntimeError(
                "This database is stamped at a revision this build does not know - the running "
                "code is OLDER than the schema (a rollback past the rollback floor, or a stale "
                "checkout). Deploy a release at least as new as the database. No migration ran."
            ) from exc
        print(f"treg schema: alembic upgrade head ({state} database)")
        return

    raise RuntimeError(
        "This database predates Alembic adoption. Install the adoption release - `pip install "
        "'tools-registry[server]==0.14.*'` - run `python -m treg upgrade` there to adopt and "
        "stamp it, then upgrade onward. Nothing was changed."
    )


async def upgrade() -> None:
    """Prepare the schema, then run every idempotent release task in order."""
    await _upgrade_schema()
    logger = logging.getLogger("treg.maintenance")
    for name, task in RELEASE_TASKS:
        changed = await task()
        logger.info("upgrade task %s complete: %d row(s) created", name, changed)
