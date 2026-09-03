"""Async database engine, read-only startup verification, and test schema reset."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from functools import cache
from importlib import import_module

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from ..config import get_settings

# `expire_on_commit=False` so objects stay usable after commit without a reload round-trip.
# On a real (non-SQLite) DB, add production pool hygiene: pre-ping to drop dead connections
# (Postgres/PgBouncer close idle ones, otherwise a post-idle request 500s) and a recycle window,
# sized against the relay's concurrency so bursts don't starve the pool and time out.
_db_url = get_settings().database_url
_is_sqlite = "sqlite" in _db_url


# THREE POOLS, ONE DATABASE — a bulkhead. Each class of work can exhaust only its own slots, so
# admin pages and background writers can no longer starve the API. Sizing is deliberately lopsided:
# the API is the product, the other two are not. Overflow is 0 on both minor pools because overflow
# is the escape hatch a bulkhead must not have. A pool, not a semaphore: a semaphore bounds only the
# module that remembers to take one, a pool bounds every module routed to it.
#
# `background` is sized from `BACKGROUND_CONSUMERS` below, NOT from a number someone picked. The
# first cut of this sized it as `audit(4) + archive(4)` and shipped a test asserting exactly that
# sum — which passed while four more consumers drew on the same slots, one of them (`adsconv`)
# holding one across two Google round trips. A guard that names a property it does not check is
# worse than no guard, so the list is the source of truth and the test walks it.
#
# `admin` is 3: one slot for a staff page left polling, one for a human using the dashboard, one
# spare. Any handler that opens a SECOND session eats two, which is how `/admin/errors` ate the
# whole pool when this was 2 — see `_purge_expired_error_evidence`, now on `background` and
# single-flighted so concurrent readers cannot multiply it.
#
# Sizes are PER INSTANCE and a rolling deploy runs two, so the SUM is what must stay under the
# database plan's ~100 ceiling — see ops/deploy.md, and the guard test.
#
# These numbers can only be validated in production: too small and real traffic gets 503s, too large
# and the bulkhead is decorative, and no test can tell you which. `TREG_DB_POOL_OVERRIDES` makes a
# wrong one a dashboard edit rather than a deploy.

# Everything that can hold a `background` slot at the same moment. Keep this in step with reality:
# it is what sizes the pool, and a consumer missing from it is a row silently dropped under load.
BACKGROUND_CONSUMERS: dict[str, int] = {
    "audit._write": 4,            # bounded by audit._MAX_CONCURRENT_WRITES
    "archive._store/_touch": 4,   # bounded by archive._MAX_CONCURRENT_WRITES (one shared semaphore)
    "adsconv.worker": 1,          # holds its slot across two Google round trips — see follow-ups
    "archive.prune_worker": 1,    # holds one across a whole sweep
    "archive.refresh_worker": 1,
    "catalog observation refresh": 1,   # singleflight, one task per process
    "admin evidence sweep": 1,    # single-flighted in routers/admin.py
}

POOL_SPECS: dict[str, dict[str, int]] = {
    "api":        {"pool_size": 5, "max_overflow": 10},
    "admin":      {"pool_size": 3, "max_overflow": 0},
    "background": {"pool_size": sum(BACKGROUND_CONSUMERS.values()), "max_overflow": 0},
}

# A pool of 0 or a negative overflow means UNLIMITED to SQLAlchemy, not "off" — `pool_size=0`
# silently sets `_max_overflow=-1`. Someone typing `-1` mid-incident to mean "no overflow" would
# uncap connections against the ~100 ceiling: the 2026-08-15 outage, entered through the knob added
# to prevent outages. So the bounds are enforced here rather than trusted.
_OVERRIDE_FLOORS = {"pool_size": 1, "max_overflow": 0}


def _apply_overrides(specs: dict[str, dict[str, int]], raw: str) -> dict[str, dict[str, int]]:
    """`"admin.pool_size=4,background.pool_size=12"` → patched specs. Anything unknown, malformed or
    out of range is logged and skipped rather than raised: this knob gets reached for during an
    incident, and a typo in it must not be what stops the server from booting. But it must SAY so —
    a silently ignored override leaves an operator believing they resized the pool."""
    log = logging.getLogger("treg")
    for item in (part.strip() for part in raw.split(",") if part.strip()):
        path, sep, value = item.partition("=")
        pool, _, field = path.strip().partition(".")
        floor = _OVERRIDE_FLOORS.get(field)
        if not sep or pool not in specs or floor is None:
            log.warning("ignoring TREG_DB_POOL_OVERRIDES entry %r: unknown pool or field", item)
            continue
        try:
            parsed = int(value)
        except ValueError:
            log.warning("ignoring TREG_DB_POOL_OVERRIDES entry %r: not an integer", item)
            continue
        if parsed < floor:
            log.warning("ignoring TREG_DB_POOL_OVERRIDES entry %r: %s must be >= %d (SQLAlchemy "
                        "reads a lower value as UNLIMITED)", item, field, floor)
            continue
        specs[pool][field] = parsed
    return specs


if _overrides := get_settings().db_pool_overrides:
    POOL_SPECS = _apply_overrides(POOL_SPECS, _overrides)


def _new_engine(name: str):
    """One pooled engine per `POOL_SPECS` entry.

    `pool_timeout` is 5 s, not SQLAlchemy's default 30: a request that cannot get a slot is treg
    saturated, and the caller should hear that fast and typed (`bootstrap_handlers` maps the
    TimeoutError to a `503 treg_saturated` with Retry-After) rather than wait 30 s for an anonymous
    500. Slots only bound concurrent DB PHASES, which are milliseconds — a `/call/` holds no
    connection during its upstream round trip (`call_tool` commits before `relay()`)."""
    spec = POOL_SPECS[name]
    kwargs: dict = {"future": True}
    if not _is_sqlite:
        kwargs.update(
            pool_pre_ping=True, pool_recycle=300, pool_timeout=5,
            pool_size=spec["pool_size"], max_overflow=spec["max_overflow"],
        )
    return create_async_engine(_db_url, **kwargs)


_engine = _new_engine("api")
_admin_engine = _new_engine("admin")
_background_engine = _new_engine("background")

if _is_sqlite:
    # No pool to protect, and file-level write locks it cannot share: three engines against one file
    # would only manufacture "database is locked". Tests pin the routing, not the isolation.
    _admin_engine = _background_engine = _engine

_engines = (_engine, _admin_engine, _background_engine)

# The API pool: every request handler, through `get_session` or directly.
session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
# The admin pool: `/admin/*` only, through `get_admin_session`.
admin_session_maker = async_sessionmaker(_admin_engine, class_=AsyncSession, expire_on_commit=False)
# The background pool: writers that run off the request path and that no caller is awaiting.
background_session_maker = async_sessionmaker(
    _background_engine, class_=AsyncSession, expire_on_commit=False)


@cache
def _schema_info() -> tuple[str, frozenset[str]]:
    from alembic.script import ScriptDirectory

    # Dynamic and lazy to avoid both a runtime cycle and a static domain dependency: maintenance
    # imports this engine and also owns the one packaged Alembic Config.
    alembic_config = import_module("treg.maintenance")._alembic_config
    scripts = ScriptDirectory.from_config(alembic_config())
    head = scripts.get_current_head()
    known = frozenset(revision.revision for revision in scripts.walk_revisions())
    return head, known


def _current_revision(sync_connection) -> str | None:
    inspector = inspect(sync_connection)
    if "alembic_version" not in inspector.get_table_names():
        return None
    revisions = sync_connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    return revisions[0] if len(revisions) == 1 else None


async def verify_db() -> None:
    """Verify configuration and schema compatibility without writing to the database."""
    from .. import models  # noqa: F401 - populate SQLModel.metadata for app subsystems

    # With no TREG_SECRET_KEY, crypto uses a per-process ephemeral Fernet key, so every stored
    # secret becomes undecryptable after restart. Local SQLite may use it; a real DB must not.
    settings = get_settings()
    if not settings.secret_key and "sqlite" not in settings.database_url:
        raise RuntimeError(
            "TREG_SECRET_KEY is not set on a non-SQLite database - stored secrets would be lost on "
            "the next restart. Set a key (treg keygen) before starting."
        )
    if not settings.secret_key:
        logging.getLogger("treg").warning(
            "TREG_SECRET_KEY unset - using an EPHEMERAL key; stored secrets will not survive a restart."
        )

    async with _engine.connect() as connection:
        revision = await connection.run_sync(_current_revision)

    if revision is None:
        raise RuntimeError(
            "Database schema is not initialized or was not adopted - run `python -m treg upgrade`. "
            "For a pre-adoption database, first install `tools-registry[server]==0.14.*` and run "
            "the command there."
        )

    head, known = _schema_info()
    if revision not in known:
        logging.getLogger("treg").warning(
            "Database revision %s is unknown to this build; running code is older than the schema; "
            "additive revisions tolerate this, a contract revision does not.",
            revision,
        )
        return
    if revision != head:
        raise RuntimeError(
            f"Database schema revision {revision} is behind this build ({head}) - run "
            "`python -m treg upgrade`."
        )


async def _stamp_test_schema(connection) -> None:
    head, _ = _schema_info()
    await connection.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(32) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    ))
    await connection.execute(text("DELETE FROM alembic_version"))
    await connection.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
        {"head": head},
    )


async def reset_db() -> None:
    """Give each test a clean, head-stamped registry while preserving a Postgres schema."""
    from .. import models  # noqa: F401 - populate SQLModel.metadata

    for engine in dict.fromkeys(_engines):  # de-duplicated: sqlite aliases all three to one
        await engine.dispose()
    async with _engine.begin() as connection:
        if _is_sqlite:
            await connection.run_sync(SQLModel.metadata.drop_all)
            await connection.run_sync(SQLModel.metadata.create_all)
        else:
            expected = {table.name for table in SQLModel.metadata.sorted_tables}
            existing = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            if expected.issubset(existing):
                # Rebuilding dozens of tables and indexes at every test boundary floods a
                # containerized Postgres with DDL and WAL. TRUNCATE preserves the production-shaped
                # schema while rows and identities reset.
                quote = connection.dialect.identifier_preparer.quote
                tables = ", ".join(quote(name) for name in sorted(expected))
                await connection.execute(text(
                    f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"
                ))
            else:
                # Schema-specific tests may deliberately remove tables. Rebuild the complete current
                # shape in that case; ordinary Postgres test boundaries take the TRUNCATE path.
                await connection.run_sync(SQLModel.metadata.drop_all)
                await connection.run_sync(SQLModel.metadata.create_all)

        await _stamp_test_schema(connection)


async def dispose_engine() -> None:
    for engine in dict.fromkeys(_engines):
        await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_maker() as session:
        yield session


async def get_admin_session() -> AsyncIterator[AsyncSession]:
    """The `/admin/*` dependency. A separate callable, not a flag, because FastAPI caches a
    dependency per request by identity: the admin gate and the handler it guards must name the SAME
    one, or one admin request checks out a connection from each of two pools."""
    async with admin_session_maker() as session:
        yield session
