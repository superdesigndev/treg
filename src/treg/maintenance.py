"""Explicit, ordered maintenance tasks run once per release before serving."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from importlib.resources import files

from alembic import command, util
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

from .application.connect import _backfill_provider_extra_tools
from .infra.db import _db_url, _engine


ReleaseTask = tuple[str, Callable[[], Awaitable[int]]]

# Content-driven tasks stay in a stable order so every release applies the same repairs before
# new code serves traffic. Task bodies remain with their owning subsystem; this is orchestration.
RELEASE_TASKS: tuple[ReleaseTask, ...] = (
    ("provider companion tools", _backfill_provider_extra_tools),
)

# A hot-table ALTER queues behind live traffic for its ACCESS EXCLUSIVE lock, and every later
# query on that table queues behind IT, so `alembic/env.py` caps each wait at 5 s. Waiting longer
# on one attempt would lengthen that stall; retrying keeps every stall at 5 s while the deploy as a
# whole waits long enough for the short transactions on the table to drain. env.py commits each
# revision on its own, so a retry resumes at the revision that timed out.
LOCK_RETRY_ATTEMPTS = 12
LOCK_RETRY_PAUSE_SECONDS = 5.0

logger = logging.getLogger("treg.maintenance")


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


def _is_lock_timeout(exc: BaseException) -> bool:
    """PostgreSQL cancelled a statement because `lock_timeout` elapsed (asyncpg's
    `LockNotAvailableError`), wrapped however SQLAlchemy chose to wrap it."""
    if not isinstance(exc, DBAPIError):
        return False
    origin = exc.orig
    return "lock timeout" in str(origin or exc) or (
        origin is not None and "LockNotAvailableError" in type(origin).__name__
    )


async def _upgrade_head_with_lock_retry(config: Config) -> None:
    for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(command.upgrade, config, "head")
            return
        except DBAPIError as exc:
            if not _is_lock_timeout(exc) or attempt == LOCK_RETRY_ATTEMPTS:
                raise
            pause = random.uniform(LOCK_RETRY_PAUSE_SECONDS / 2, LOCK_RETRY_PAUSE_SECONDS)
            logger.warning(
                "migration lock timeout on attempt %d/%d; retrying in %.1f s",
                attempt, LOCK_RETRY_ATTEMPTS, pause,
            )
            await asyncio.sleep(pause)


async def _upgrade_schema() -> None:
    tables = await _table_names()
    config = _alembic_config()

    if not tables or "alembic_version" in tables:
        state = "empty" if not tables else "stamped"
        try:
            await _upgrade_head_with_lock_retry(config)
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
    for name, task in RELEASE_TASKS:
        changed = await task()
        logger.info("upgrade task %s complete: %d row(s) created", name, changed)
