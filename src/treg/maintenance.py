"""Explicit, ordered maintenance tasks run once per release before serving."""

from __future__ import annotations

import ast
import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from importlib.resources import files

from alembic import command, util
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from .application.connect import _backfill_provider_extra_tools
from .config import get_settings
from .infra.db import _db_url, _engine


ReleaseTask = tuple[str, Callable[[], Awaitable[int]]]

# Content-driven tasks stay in a stable order so every release applies the same repairs before
# new code serves traffic. Task bodies remain with their owning subsystem; this is orchestration.
RELEASE_TASKS: tuple[ReleaseTask, ...] = (
    ("provider companion tools", _backfill_provider_extra_tools),
)

# PostgreSQL shows another role's query as this text (or NULL) to a role without
# pg_read_all_stats.
_HIDDEN_QUERY = "<insufficient privilege>"

_LONG_LOCK_HOLDERS = text("""
    SELECT a.pid, c.relname, l.mode, a.backend_type, a.query,
           EXTRACT(EPOCH FROM now() - a.xact_start) AS xact_s
    FROM pg_locks l
    JOIN pg_class c ON c.oid = l.relation
    JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.granted AND l.locktype = 'relation'
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND pg_table_is_visible(c.oid)
      AND c.relname = ANY(:tables)
      AND a.pid <> pg_backend_pid()
      AND (a.backend_type = 'autovacuum worker'
           OR a.xact_start < now() - make_interval(secs => :max_xact_s))
    ORDER BY c.relname, a.pid
""")

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
    settings = get_settings()
    attempts = settings.migration_lock_retry_attempts
    pause_s = settings.migration_lock_retry_pause_s
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(command.upgrade, config, "head")
            return
        except DBAPIError as exc:
            if not _is_lock_timeout(exc) or attempt == attempts:
                raise
            pause = random.uniform(pause_s / 2, pause_s)
            logger.warning(
                "migration lock timeout on attempt %d/%d; retrying in %.1f s",
                attempt, attempts, pause,
            )
            await asyncio.sleep(pause)


def _revision_strings(path: str) -> list[str]:
    """Every string literal in a revision file except docstrings: the table names its operations
    and raw SQL use, without the prose that mentions other tables."""
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read())
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


async def _pending_revision_tables(config: Config, tables: set[str]) -> set[str]:
    """Existing tables named in a revision `upgrade head` would apply. Empty when nothing is
    pending, or when the stamp is unknown (the upgrade itself then refuses with its message)."""
    async with _engine.connect() as connection:
        current = await connection.run_sync(
            lambda sync_connection: MigrationContext.configure(sync_connection).get_current_heads()
        )
    return _tables_named_by_pending(ScriptDirectory.from_config(config), current, tables)


def _tables_named_by_pending(script: ScriptDirectory, current: tuple[str, ...],
                             tables: set[str]) -> set[str]:
    try:
        pending = list(script.iterate_revisions("heads", current or "base"))
    except (RevisionError, util.CommandError):
        return set()
    if not pending or not tables:
        return set()
    word = re.compile(r"\b(" + "|".join(map(re.escape, sorted(tables))) + r")\b")
    return {match for revision in pending if revision.path
            for literal in _revision_strings(revision.path)
            for match in word.findall(literal)}


def _long_holder(row) -> str | None:
    if row.backend_type != "autovacuum worker":
        return (f"pid {row.pid} ({row.backend_type}, transaction open {row.xact_s:.0f} s) "
                f"holds {row.mode} on {row.relname}")
    if row.query in (None, "", _HIDDEN_QUERY):
        logger.warning(
            "migration preflight cannot tell whether autovacuum pid %d on %s is an "
            "anti-wraparound pass: this role cannot read its query (grant pg_read_all_stats); "
            "continuing", row.pid, row.relname,
        )
        return None
    if "to prevent wraparound" not in row.query:
        return None
    return f"pid {row.pid} (autovacuum to prevent wraparound) holds {row.mode} on {row.relname}"


async def _refuse_behind_long_lock_holder(config: Config, tables: set[str]) -> None:
    """Read-only, before any lock is requested. PostgreSQL cancels an ordinary autovacuum that
    blocks a waiting ALTER, but never an anti-wraparound one, and a long transaction keeps its
    locks until it ends; retrying behind either only repeats the stall on every later query."""
    settings = get_settings()
    if not settings.migration_lock_holder_check or _engine.dialect.name != "postgresql":
        return
    touched = await _pending_revision_tables(config, tables)
    if not touched:
        return
    async with _engine.connect() as connection:
        rows = (await connection.execute(_LONG_LOCK_HOLDERS, {
            "tables": sorted(touched), "max_xact_s": settings.migration_lock_holder_max_xact_s,
        })).all()
    holders = [found for found in map(_long_holder, rows) if found]
    if holders:
        raise RuntimeError(
            "Refusing to upgrade: a pending revision needs a lock a long-running session holds, "
            "and waiting for it would stall every query on that table on each retry. "
            + "; ".join(holders) + ". Wait for it to finish (never cancel an anti-wraparound "
            "vacuum) or end that transaction, then run `python -m treg upgrade` again. "
            "No migration ran."
        )


async def _upgrade_schema() -> None:
    tables = await _table_names()
    config = _alembic_config()

    if not tables or "alembic_version" in tables:
        state = "empty" if not tables else "stamped"
        await _refuse_behind_long_lock_holder(config, tables)
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
