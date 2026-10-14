"""Finish money-session cleanup before an enclosing admission lease can be released."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def money_session(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Use the caller's session and commit boundaries, joining close under repeated cancellation.

    AsyncSession's normal exit shields close but a second cancellation stops awaiting it. Money
    admission must remain owned until that close (and any rollback) has actually completed.
    """
    failure: BaseException | None = None
    try:
        yield db
    except BaseException as exc:
        failure = exc
        raise
    finally:
        cleanup = asyncio.create_task(db.close())
        cancelled: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:  # Inspect the cleanup result without replacing an original error.
                break
        try:
            cleanup.result()
        except BaseException:
            if failure is None:
                raise
            logging.getLogger("treg.ledger").exception("money session cleanup failed")
        if failure is None and cancelled is not None:
            raise cancelled
