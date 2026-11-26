"""A connection the pool closes is closed on the server too, even when its close is cancelled."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.util import greenlet_spawn

from treg.infra import db as infra_db


class _Transport:
    def __init__(self, closing: bool = False) -> None:
        self.closing = closing
        self.aborted = 0

    def is_closing(self) -> bool:
        return self.closing

    def abort(self) -> None:
        self.aborted += 1
        self.closing = True


class _Driver:
    def __init__(self, transport: _Transport) -> None:
        self._transport = transport


class _Connection:
    def __init__(self, transport: _Transport) -> None:
        self.driver_connection = _Driver(transport)


class _Dialect:
    def __init__(self, failure: BaseException | None) -> None:
        self.failure = failure

    def do_terminate(self, dbapi_connection) -> None:
        if self.failure is not None:
            raise self.failure

    def do_close(self, dbapi_connection) -> None:
        if self.failure is not None:
            raise self.failure


@pytest.mark.parametrize("name", ["do_terminate", "do_close"])
def test_an_interrupted_close_still_closes_the_socket_and_keeps_the_cancellation(name):
    dialect = _Dialect(asyncio.CancelledError())
    infra_db._abort_socket_after_close(dialect)
    transport = _Transport()

    with pytest.raises(asyncio.CancelledError):
        getattr(dialect, name)(_Connection(transport))
    assert transport.aborted == 1


@pytest.mark.parametrize("name", ["do_terminate", "do_close"])
def test_a_socket_already_closing_is_left_alone(name):
    dialect = _Dialect(None)
    infra_db._abort_socket_after_close(dialect)
    transport = _Transport(closing=True)

    getattr(dialect, name)(_Connection(transport))
    assert transport.aborted == 0


async def _cancelled_mid_close(self) -> None:
    """asyncpg's own close, interrupted: it marks the protocol closing, then a cancellation lands
    while it waits for the cancel of an in-flight statement to be sent."""
    closing = asyncio.ensure_future(self._connection.close(timeout=2))
    await asyncio.sleep(0)
    closing.cancel()
    await closing


_LOCK_KEY = 0x7472_6567_0001


async def _session_state(server_pid: int) -> str | None:
    async with infra_db._engine.connect() as connection:
        return (await connection.execute(
            text("SELECT coalesce(wait_event_type, state) FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": server_pid})).scalar_one_or_none()


@pytest.mark.skipif(infra_db._is_sqlite, reason="needs asyncpg against PostgreSQL")
async def test_a_terminate_cancelled_mid_close_leaves_no_session_on_the_server(
    load_db_module, monkeypatch,
):
    module = load_db_module(database_url=infra_db._db_url)
    engine = module._engine
    raw = await engine.raw_connection()
    dbapi_connection, driver = raw.dbapi_connection, raw.driver_connection
    server_pid = driver.get_server_pid()

    # Another session holds the lock, so the statement is still running when the close is
    # interrupted, however slow the machine.
    async with infra_db._engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _LOCK_KEY})
        try:
            await driver.execute("BEGIN")
            in_flight = asyncio.ensure_future(
                driver.fetchval("SELECT pg_advisory_xact_lock($1)", _LOCK_KEY))
            async with asyncio.timeout(5):
                while await _session_state(server_pid) != "Lock":
                    await asyncio.sleep(0.01)
            monkeypatch.setattr(AsyncAdapt_asyncpg_connection, "_terminate_graceful_close",
                                _cancelled_mid_close)

            with pytest.raises(asyncio.CancelledError):
                await greenlet_spawn(engine.dialect.do_terminate, dbapi_connection)
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
            await holder.commit()

    with contextlib.suppress(BaseException):
        await in_flight
    # The server notices a closed socket once the statement it was running ends; an open one
    # leaves the session idle in transaction for good.
    state = await _session_state(server_pid)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(5):
            while state is not None:
                await asyncio.sleep(0.05)
                state = await _session_state(server_pid)
    await greenlet_spawn(raw.invalidate)
    assert state is None
