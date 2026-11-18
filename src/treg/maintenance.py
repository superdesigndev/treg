"""Explicit, ordered maintenance tasks run once per release before serving."""

from __future__ import annotations

import ast
import asyncio
import logging
import random
import re
import time
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

# pg_locks and pg_class are readable by every role; pg_stat_activity hides backend_type,
# xact_start and query of another role's sessions (NULL) from a role without pg_read_all_stats,
# autovacuum included. Such a row is kept and judged by its lock and by the table's freeze age.
# A lock on a partition or a TOAST table blocks an ALTER of its parent the same way, and a
# table's own autovacuum_freeze_max_age reloption can only lower the server setting.
_LONG_LOCK_HOLDERS = text("""
    WITH named AS (
        SELECT oid, relname FROM pg_class
        WHERE relname = ANY(:tables) AND pg_table_is_visible(oid)
    ), tree AS (
        SELECT oid, relname AS parent FROM named
        UNION
        SELECT t.relid, n.relname FROM named n CROSS JOIN LATERAL pg_partition_tree(n.oid) t
    ), rels AS (
        SELECT oid, parent FROM tree
        UNION
        SELECT c.reltoastrelid, t.parent FROM tree t JOIN pg_class c ON c.oid = t.oid
        WHERE c.reltoastrelid <> 0
    )
    SELECT a.pid, l.mode, a.backend_type, a.query,
           CASE WHEN c.relname = r.parent THEN c.relname
                ELSE c.relname || ' (part of ' || r.parent || ')' END AS relname,
           EXTRACT(EPOCH FROM now() - a.xact_start) AS xact_s,
           CASE WHEN c.relkind IN ('r', 'm', 't') THEN
               age(c.relfrozenxid) >= LEAST(
                   current_setting('autovacuum_freeze_max_age')::bigint,
                   COALESCE((SELECT o.option_value::bigint FROM pg_options_to_table(c.reloptions) o
                             WHERE o.option_name = 'autovacuum_freeze_max_age'),
                            current_setting('autovacuum_freeze_max_age')::bigint))
               OR mxid_age(c.relminmxid) >= LEAST(
                   current_setting('autovacuum_multixact_freeze_max_age')::bigint,
                   COALESCE((SELECT o.option_value::bigint FROM pg_options_to_table(c.reloptions) o
                             WHERE o.option_name = 'autovacuum_multixact_freeze_max_age'),
                            current_setting('autovacuum_multixact_freeze_max_age')::bigint))
           ELSE false END AS past_freeze_age
    FROM pg_locks l
    JOIN rels r ON r.oid = l.relation
    JOIN pg_class c ON c.oid = l.relation
    JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.granted AND l.locktype = 'relation'
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND l.mode = ANY(:modes)
      AND a.pid <> pg_backend_pid()
      AND (a.backend_type IS NULL
           OR a.backend_type = 'autovacuum worker'
           OR a.xact_start < now() - make_interval(secs => :max_xact_s))
    ORDER BY r.parent, c.relname, a.pid
""")

# PostgreSQL's table-level lock conflicts for the two modes a revision takes: DDL takes ACCESS
# EXCLUSIVE, which conflicts with every mode; plain UPDATE/INSERT/DELETE take ROW EXCLUSIVE.
_ALL_LOCK_MODES = (
    "AccessShareLock", "RowShareLock", "RowExclusiveLock", "ShareUpdateExclusiveLock",
    "ShareLock", "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock",
)
_CONFLICTS_WITH_ROW_EXCLUSIVE = (
    "ShareLock", "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock",
)
_DATA_OPS = frozenset({"execute", "bulk_insert", "inline_literal"})
_SQL_EXPRESSIONS = frozenset({"text", "table", "column"})
_DATA_SQL = re.compile(r"\s*(UPDATE|INSERT|DELETE|SELECT|WITH)\b", re.IGNORECASE)
_DDL_SQL = re.compile(
    r"\b(ALTER|DROP|CREATE|LOCK|TRUNCATE|VACUUM|CLUSTER|REINDEX|COMMENT|GRANT|REVOKE|DO)\b",
    re.IGNORECASE)
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

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


async def _upgrade_head_with_lock_retry(config: Config, tables: set[str]) -> None:
    """`alembic upgrade head`, retried after a lock timeout. The long-holder check runs before
    every attempt, since a holder can start between attempts, and retries stop once
    `migration_lock_retry_budget_s` would be passed."""
    settings = get_settings()
    attempts = settings.migration_lock_retry_attempts
    pause_s = settings.migration_lock_retry_pause_s
    budget_s = settings.migration_lock_retry_budget_s
    bypass = _holder_check_bypass(config)
    if bypass:
        logger.warning("migration long-lock-holder check is OFF for this upgrade: %s", bypass)
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        await _refuse_behind_long_lock_holder(config, tables)
        try:
            await asyncio.to_thread(command.upgrade, config, "head")
            return
        except DBAPIError as exc:
            if not _is_lock_timeout(exc) or attempt == attempts:
                raise
            pause = random.uniform(pause_s / 2, pause_s)
            elapsed = time.monotonic() - started
            if elapsed + pause > budget_s:
                raise RuntimeError(
                    f"Migration lock retries stopped after {elapsed:.0f} s and {attempt} "
                    f"attempt(s): the retry time budget (TREG_MIGRATION_LOCK_RETRY_BUDGET_S="
                    f"{budget_s:g}) is spent. Revisions committed before the timeout stay "
                    "applied; find what holds the lock, then run `python -m treg upgrade` again."
                ) from exc
            logger.warning(
                "migration lock timeout on attempt %d/%d; retrying in %.1f s",
                attempt, attempts, pause,
            )
            await asyncio.sleep(pause)


def _revision_strings(path: str) -> list[str]:
    """Every string literal in a revision file except docstrings and error messages: the table
    names its operations and raw SQL use, without the prose that mentions other tables."""
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read())
    skipped = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    } | {id(inner) for node in ast.walk(tree) if isinstance(node, ast.Raise)
         for inner in ast.walk(node)}
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in skipped]


async def _pending_revision_tables(config: Config,
                                   tables: set[str]) -> tuple[set[str], bool]:
    """Existing tables named in a revision `upgrade head` would apply, and whether any of those
    revisions takes ACCESS EXCLUSIVE. No tables when nothing is pending, or when the stamp is
    unknown (the upgrade itself then refuses with its message)."""
    async with _engine.connect() as connection:
        current = await connection.run_sync(
            lambda sync_connection: MigrationContext.configure(sync_connection).get_current_heads()
        )
    script = ScriptDirectory.from_config(config)
    return (_tables_named_by_pending(script, current, tables),
            any(map(_takes_access_exclusive, _pending_paths(script, current))))


def _pending_paths(script: ScriptDirectory, current: tuple[str, ...]) -> list[str]:
    try:
        pending = list(script.iterate_revisions("heads", current or "base"))
    except (RevisionError, util.CommandError):
        return []
    return [revision.path for revision in pending if revision.path]


def _tables_named_by_pending(script: ScriptDirectory, current: tuple[str, ...],
                             tables: set[str]) -> set[str]:
    paths = _pending_paths(script, current)
    if not paths or not tables:
        return set()
    word = re.compile(r"\b(" + "|".join(map(re.escape, sorted(tables))) + r")\b")
    return {match for path in paths for literal in _revision_strings(path)
            for match in word.findall(literal)}


def _reads_as_ddl(literal: str) -> bool:
    return bool(_DDL_SQL.search(_SQL_COMMENT.sub(" ", literal)))


def _takes_access_exclusive(path: str) -> bool:
    """False only for a revision that plainly changes rows, read fail-closed: in `upgrade()` and
    the module functions it calls, every call is a data `op.*` call (`execute` of literal
    UPDATE/INSERT/DELETE/SELECT SQL, `bulk_insert`), a `sa.text`/`sa.table`/`sa.column`
    expression, or another module function; and no string there or in a module constant names a
    DDL keyword anywhere, SQL comments removed. Anything else is taken to need ACCESS EXCLUSIVE."""
    with open(path, encoding="utf-8") as source:
        module = ast.parse(source.read())
    functions = {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}
    if "upgrade" not in functions:
        return True
    constants = [node for node in module.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    if any(isinstance(node, ast.Constant) and isinstance(node.value, str)
           and _reads_as_ddl(node.value) for constant in constants for node in ast.walk(constant)):
        return True
    reached, queue = {"upgrade"}, [functions["upgrade"]]
    while queue:
        for node in ast.walk(queue.pop()):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and _reads_as_ddl(node.value):
                return True
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in functions:
                if func.id not in reached:
                    reached.add(func.id)
                    queue.append(functions[func.id])
                continue
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                return True
            if func.value.id in ("sa", "sqlalchemy") and func.attr in _SQL_EXPRESSIONS:
                continue
            if func.value.id != "op" or func.attr not in _DATA_OPS:
                return True
            if func.attr == "execute":
                argument = node.args[0] if node.args else None
                if isinstance(argument, ast.Call) and argument.args:
                    argument = argument.args[0]
                if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                        and _DATA_SQL.match(_SQL_COMMENT.sub(" ", argument.value))):
                    return True
    return False


def _head_revision(config: Config) -> str | None:
    return ScriptDirectory.from_config(config).get_current_head()


def _holder_check_bypass_quiet(config: Config) -> bool:
    settings = get_settings()
    skip = settings.migration_lock_holder_check_skip
    return not settings.migration_lock_holder_check or bool(skip and skip == _head_revision(config))


def _holder_check_bypass(config: Config) -> str | None:
    """Why the long-holder check is off for this upgrade, or None when it runs."""
    settings = get_settings()
    if not settings.migration_lock_holder_check:
        return ("TREG_MIGRATION_LOCK_HOLDER_CHECK=false; remove it after this deploy, or use "
                "TREG_MIGRATION_LOCK_HOLDER_CHECK_SKIP=<head revision>, which ends by itself")
    skip = settings.migration_lock_holder_check_skip
    if not skip:
        return None
    head = _head_revision(config)
    if skip == head:
        return (f"TREG_MIGRATION_LOCK_HOLDER_CHECK_SKIP={skip} matches the head revision; it "
                "stops applying once a later revision ships")
    logger.warning(
        "TREG_MIGRATION_LOCK_HOLDER_CHECK_SKIP=%s does not match the head revision %s; the check "
        "runs (remove the setting)", skip, head,
    )
    return None


def _long_holder(row) -> str | None:
    if row.backend_type is None:
        # Another role's session this role cannot see: its age and kind are unknown. A
        # SHARE UPDATE EXCLUSIVE lock on a table past its freeze age is what an anti-wraparound
        # autovacuum holds; anything else is left to the bounded lock wait.
        if row.mode == "ShareUpdateExclusiveLock" and row.past_freeze_age:
            return (f"pid {row.pid} (hidden from this role; on a table past its freeze age, "
                    f"most likely an autovacuum to prevent wraparound) holds {row.mode} "
                    f"on {row.relname}")
        logger.warning(
            "migration preflight cannot see pid %d holding %s on %s: it belongs to another "
            "role and this role lacks pg_read_all_stats, so its age and kind are unknown; "
            "continuing", row.pid, row.mode, row.relname,
        )
        return None
    if row.backend_type != "autovacuum worker":
        return (f"pid {row.pid} ({row.backend_type}, transaction open {row.xact_s:.0f} s) "
                f"holds {row.mode} on {row.relname}")
    if row.query in (None, "", _HIDDEN_QUERY):
        if row.past_freeze_age:
            return (f"pid {row.pid} (autovacuum on a table past its freeze age) "
                    f"holds {row.mode} on {row.relname}")
        logger.warning(
            "migration preflight cannot read the query of autovacuum pid %d on %s "
            "(grant pg_read_all_stats); the table is not past its freeze age, continuing",
            row.pid, row.relname,
        )
        return None
    if "to prevent wraparound" not in row.query:
        return None
    return f"pid {row.pid} (autovacuum to prevent wraparound) holds {row.mode} on {row.relname}"


async def _refuse_behind_long_lock_holder(config: Config, tables: set[str]) -> None:
    """Read-only, before any lock is requested. PostgreSQL cancels an ordinary autovacuum that
    blocks a waiting ALTER, but never an anti-wraparound one, and a long transaction keeps its
    locks until it ends; retrying behind either only repeats the stall on every later query.
    Only a holder whose lock conflicts with what the pending revisions take counts."""
    settings = get_settings()
    if _engine.dialect.name != "postgresql" or _holder_check_bypass_quiet(config):
        return
    touched, access_exclusive = await _pending_revision_tables(config, tables)
    if not touched:
        return
    modes = _ALL_LOCK_MODES if access_exclusive else _CONFLICTS_WITH_ROW_EXCLUSIVE
    async with _engine.connect() as connection:
        rows = (await connection.execute(_LONG_LOCK_HOLDERS, {
            "tables": sorted(touched), "modes": list(modes),
            "max_xact_s": settings.migration_lock_holder_max_xact_s,
        })).all()
    holders = [found for found in map(_long_holder, rows) if found]
    if holders:
        raise RuntimeError(
            "Refusing to upgrade: a pending revision needs a lock a long-running session holds, "
            "and waiting for it would stall every query on that table on each retry. "
            + "; ".join(holders) + ". Wait for it to finish (never cancel an anti-wraparound "
            "vacuum) or end that transaction, then run `python -m treg upgrade` again. To "
            "upgrade anyway behind the bounded lock wait for this one deploy, set "
            f"TREG_MIGRATION_LOCK_HOLDER_CHECK_SKIP={_head_revision(config)}; it ends by itself "
            "at the next revision. Nothing was applied after this check."
        )


async def _upgrade_schema() -> None:
    tables = await _table_names()
    config = _alembic_config()

    if not tables or "alembic_version" in tables:
        state = "empty" if not tables else "stamped"
        try:
            await _upgrade_head_with_lock_retry(config, tables)
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
