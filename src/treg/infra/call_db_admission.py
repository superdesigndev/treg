"""Bound new call transactions before they compete for API pool connections."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from .db import POOL_SPECS


class CallDBAdmissionRejected(Exception):
    def __init__(self, operation: str) -> None:
        super().__init__("new call database work is saturated")
        self.operation = operation


class _Gate:
    def __init__(self, limit: int, wait_s: float = 0.05) -> None:
        self.limit = limit
        self.wait_s = wait_s
        self.waiting = 0
        self.active = 0
        self._slots = asyncio.BoundedSemaphore(limit)

    @asynccontextmanager
    async def admit(self, operation: str) -> AsyncIterator[None]:
        if not self.limit or (self._slots.locked() and self.waiting >= self.limit):
            raise CallDBAdmissionRejected(operation)
        self.waiting += 1
        try:
            try:
                async with asyncio.timeout(self.wait_s):
                    await self._slots.acquire()
            except TimeoutError as exc:
                raise CallDBAdmissionRejected(operation) from exc
        finally:
            self.waiting -= 1
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._slots.release()


def _capacity() -> int:
    spec = POOL_SPECS["api"]
    return (spec["pool_size"] + spec["max_overflow"]) // 2


_loop: asyncio.AbstractEventLoop | None = None
_current: _Gate | None = None


def _gate() -> _Gate:
    global _loop, _current
    loop = asyncio.get_running_loop()
    if _loop is not loop or _current is None:
        _loop, _current = loop, _Gate(_capacity())
    return _current


@asynccontextmanager
async def admit(operation: str) -> AsyncIterator[None]:
    async with _gate().admit(operation):
        yield


async def _rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except BaseException:
        await db.close()
        raise


@asynccontextmanager
async def identity(db: AsyncSession) -> AsyncIterator[None]:
    async with admit("identity"):
        try:
            yield
        except BaseException:
            cleanup = asyncio.create_task(_rollback(db))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - inspect the cleanup result below
                    break
            try:
                cleanup.result()
            except BaseException:
                logging.getLogger("treg").exception("call identity cleanup failed")
            raise
