"""Finish money-session cleanup before an enclosing admission lease can be released."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

# Postgres ends a session whose transaction sits idle this long, rolling it back and freeing its
# row locks. A money transaction is a handful of statements with no I/O between them, so idling
# means its process stalled while holding a team's balance or credit-block rows.
IDLE_IN_TRANSACTION_TIMEOUT = "5s"


def _bound_idle(_session, _transaction, connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            f"SET LOCAL idle_in_transaction_session_timeout = '{IDLE_IN_TRANSACTION_TIMEOUT}'")


@asynccontextmanager
async def money_session(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Use the caller's session and commit boundaries, joining close under repeated cancellation.

    Every transaction it begins is bounded by `IDLE_IN_TRANSACTION_TIMEOUT`, so a process that
    stops running mid-transaction cannot keep other requests queued on the same team's rows.

    AsyncSession's normal exit shields close but a second cancellation stops awaiting it. Money
    admission must remain owned until that close (and any rollback) has actually completed.
    """
    failure: BaseException | None = None
    sync = getattr(db, "sync_session", None)
    if sync is not None:
        event.listen(sync, "after_begin", _bound_idle)
    try:
        yield db
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if sync is not None:
            event.remove(sync, "after_begin", _bound_idle)
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
