"""Finish money-session cleanup before an enclosing admission lease can be released."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

# Postgres ends a session whose transaction sits idle this long, rolling it back and freeing its
# row locks. A money transaction is a handful of statements with no I/O between them, so idling
# means its process stalled while holding a team's balance or credit-block rows.
IDLE_IN_TRANSACTION_TIMEOUT = "5s"
# And cancels any one statement of it that runs this long, a wait for another session's row lock
# included. With the idle bound that caps how long a money transaction can keep a team's rows
# locked (PostgreSQL before 17 has no `transaction_timeout`), and how long a request queued
# behind it holds its connection. Far above any money statement's own running time.
STATEMENT_TIMEOUT = "15s"
# How long a money transaction that can come back later waits for another session's row locks.
LOCK_WAIT_MS = 1000


def _bound_transaction(_session, _transaction, connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            f"SET LOCAL idle_in_transaction_session_timeout = '{IDLE_IN_TRANSACTION_TIMEOUT}'")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


async def bound_lock_wait(db: AsyncSession) -> None:
    """Cap how long this transaction waits for another session's row locks (`LOCK_WAIT_MS`).
    PostgreSQL only: SQLite has no row locks to wait for."""
    from . import db as infra_db

    if not infra_db._is_sqlite:
        await db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_MS}ms'"))


def lock_timed_out(exc: BaseException) -> bool:
    """PostgreSQL cancelled a statement because `lock_timeout` elapsed (asyncpg's
    `LockNotAvailableError`), wrapped however SQLAlchemy chose to wrap it."""
    if not isinstance(exc, DBAPIError):
        return False
    origin = exc.orig
    return "lock timeout" in str(origin or exc) or (
        origin is not None and "LockNotAvailableError" in type(origin).__name__)


@asynccontextmanager
async def money_session(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Use the caller's session and commit boundaries, joining close under repeated cancellation.

    Every transaction it begins is bounded by `IDLE_IN_TRANSACTION_TIMEOUT` and
    `STATEMENT_TIMEOUT`, so neither a process that stops running mid-transaction nor a
    statement stuck behind another session's locks can keep other requests queued on the same
    team's rows.

    AsyncSession's normal exit shields close but a second cancellation stops awaiting it. Money
    admission must remain owned until that close (and any rollback) has actually completed.
    """
    failure: BaseException | None = None
    sync = getattr(db, "sync_session", None)
    if sync is not None:
        event.listen(sync, "after_begin", _bound_transaction)
    try:
        yield db
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if sync is not None:
            event.remove(sync, "after_begin", _bound_transaction)
        cleanup = asyncio.create_task(db.close())
        cancelled: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:  # noqa: BLE001 - cleanup.result() below inspects the outcome
                break
        try:
            cleanup.result()
        except BaseException:
            if failure is None:
                raise
            logging.getLogger("treg.ledger").exception("money session cleanup failed")
        if failure is None and cancelled is not None:
            raise cancelled
