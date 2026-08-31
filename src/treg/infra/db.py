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
_engine_kwargs: dict = {"future": True}
if "sqlite" not in _db_url:
    # 5+10, not the 20+40 this shipped with. The pool is PER INSTANCE, and a rolling deploy runs two
    # instances against one Postgres. 60 each meant 120 potential connections against a basic-plan
    # ceiling of ~100, so a deploy could starve the database with no bug anywhere. 15 is generous for
    # an async app on this plan; saturation now surfaces as our own pool queueing (visible, bounded)
    # rather than Postgres refusing connections for everyone (the 2026-08-15 outage).
    #
    # `pool_timeout` 5 s, not SQLAlchemy's default 30: a request that cannot get a slot is treg
    # saturated, and the caller should hear that fast and typed (api.py maps the TimeoutError to a
    # `503 treg_saturated` with Retry-After) rather than sit 30 s and receive an anonymous 500. The
    # slot count itself only bounds concurrent DB PHASES, which are milliseconds. A /call/ holds no
    # connection during its upstream round trip (call_tool commits before relay()). Holding one
    # there is what turned 15 concurrent calls into a 30 s deadlock on 2026-08-24.
    _engine_kwargs.update(pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=10,
                          pool_timeout=5)
_engine = create_async_engine(_db_url, **_engine_kwargs)
# Public: the audit writer opens its own session here (off the request path, rule #2).
session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


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

    await _engine.dispose()
    async with _engine.begin() as connection:
        if "sqlite" in _db_url:
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
    await _engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_maker() as session:
        yield session
