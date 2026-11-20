import asyncio
import json
import sqlite3
import time

import aiosqlite
import httpx
import pytest

from test_workbench_store import PipelineServer
from treg.infra.workbench_store import SQLiteStore, StoreError, TursoStore


def make_store(kind, path, handler=None):
    if kind == "sqlite":
        return SQLiteStore(path)
    return TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                      transport=httpx.MockTransport(handler or PipelineServer(path).handle))


def prepare_leads(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, revision INTEGER)")
        connection.execute("INSERT INTO leads VALUES (1, 1)")


@pytest.mark.parametrize("kind", ["sqlite", "http"])
@pytest.mark.parametrize("timeout", [0.5, 5, float("nan")])
def test_transaction_timeout_must_leave_commit_window(tmp_path, kind, timeout):
    with pytest.raises(StoreError, match="^invalid_configuration$"):
        if kind == "sqlite":
            SQLiteStore(tmp_path / "team.db", transaction_timeout=timeout)
        else:
            TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                       transaction_timeout=timeout)


@pytest.mark.parametrize("kind", ["sqlite", "http"])
@pytest.mark.parametrize("cancellation", ["cancel", "timeout", "taskgroup"])
async def test_commit_preserves_structured_cancellation(tmp_path, monkeypatch, kind, cancellation):
    path = tmp_path / "team.db"
    prepare_leads(path)
    entered = asyncio.Event()
    blocked = asyncio.Event()
    server = PipelineServer(path)
    original_execute = aiosqlite.Connection._execute

    async def execute(connection, operation, *args, **kwargs):
        result = await original_execute(connection, operation, *args, **kwargs)
        if operation.__name__ == "execute" and args[0] == "COMMIT":
            entered.set()
            await blocked.wait()
        return result

    async def respond(request):
        response = server.handle(request)
        if json.loads(request.content)["requests"][0]["stmt"]["sql"] == "COMMIT":
            entered.set()
            await blocked.wait()
        return response

    monkeypatch.setattr(aiosqlite.Connection, "_execute", execute)
    store = make_store(kind, path, respond)
    transactions = []
    timeouts = []

    async def write():
        async with asyncio.timeout(None) as timeout:
            timeouts.append(timeout)
            async with store.transaction() as tx:
                transactions.append(tx)
                await tx.execute("UPDATE leads SET revision = revision + 1 WHERE id = 1")

    async def fail_sibling():
        raise RuntimeError("sibling failed")

    if cancellation == "taskgroup":
        with pytest.raises(ExceptionGroup) as caught:
            async with asyncio.TaskGroup() as group:
                task = group.create_task(write())
                await asyncio.wait_for(entered.wait(), 1)
                group.create_task(fail_sibling())
        assert len(caught.value.exceptions) == 1
        assert isinstance(caught.value.exceptions[0], RuntimeError)
        assert task.cancelled()
    else:
        task = asyncio.create_task(write())
        await asyncio.wait_for(entered.wait(), 1)
        if cancellation == "timeout":
            timeouts[0].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(TimeoutError):
                await task
            assert task.cancelling() == 0
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()

    assert transactions[0].outcome_unknown is True
    assert transactions[0].closed is True
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT revision FROM leads").fetchone() == (2,)


@pytest.mark.parametrize("kind, close_fault", [
    ("sqlite", None), ("http", None), ("http", "malformed"),
])
async def test_commit_constraint_failure_is_definite(tmp_path, monkeypatch, kind, close_fault):
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    path = tmp_path / "team.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE children (parent_id INTEGER REFERENCES parents(id) "
                           "DEFERRABLE INITIALLY DEFERRED)")
    server = PipelineServer(path)

    def respond(request):
        response = server.handle(request)
        if close_fault and json.loads(request.content)["requests"][0]["stmt"]["sql"] == "COMMIT":
            payload = response.json()
            payload["results"][1] = {"type": "ok", "response": None}
            return httpx.Response(200, json=payload)
        return response

    store = make_store(kind, path, respond)
    with pytest.raises(StoreError, match="^constraint_violation$"):
        async with store.transaction() as tx:
            await tx.execute("INSERT INTO children VALUES (123)")
    assert tx.outcome_unknown is False
    assert (await store.read("SELECT * FROM children")).rows == []


@pytest.mark.parametrize("close_result", [
    {"type": "error", "error": {"code": "STREAM_CLOSED"}},
    {"type": "ok", "response": None},
])
async def test_known_commit_success_survives_close_response_failure(tmp_path, close_result):
    path = tmp_path / "team.db"
    prepare_leads(path)
    server = PipelineServer(path)

    def respond(request):
        response = server.handle(request)
        if json.loads(request.content)["requests"][0]["stmt"]["sql"] == "COMMIT":
            payload = response.json()
            payload["results"][1] = close_result
            return httpx.Response(200, json=payload)
        return response

    store = make_store("http", path, respond)
    async with store.transaction() as tx:
        await tx.execute("UPDATE leads SET revision = revision + 1 WHERE id = 1")
    assert tx.outcome_unknown is False
    assert (await store.read("SELECT revision FROM leads")).rows == [{"revision": 2}]


@pytest.mark.parametrize("kind", ["sqlite", "http"])
async def test_short_commit_window_expires_without_sending_commit(tmp_path, monkeypatch, kind):
    path = tmp_path / "team.db"
    prepare_leads(path)
    server = PipelineServer(path)
    statements = []
    original_execute = aiosqlite.Connection._execute

    async def execute(connection, operation, *args, **kwargs):
        if operation.__name__ == "execute":
            statements.append(args[0])
        return await original_execute(connection, operation, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "_execute", execute)
    store = make_store(kind, path, server.handle)
    with pytest.raises(StoreError, match="^transaction_expired$"):
        async with store.transaction() as tx:
            await tx.execute("UPDATE leads SET revision = revision + 1 WHERE id = 1")
            tx.deadline = time.monotonic() + 0.25
    if kind == "http":
        statements = [operation["stmt"]["sql"] for call in server.calls
                      for operation in call["requests"] if operation["type"] == "execute"]
    assert "COMMIT" not in statements
    assert tx.outcome_unknown is False
    assert (await store.read("SELECT revision FROM leads")).rows == [{"revision": 1}]


async def test_rollback_failure_preserves_original_store_error(tmp_path, monkeypatch):
    async def rollback(connection):
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(aiosqlite.Connection, "rollback", rollback)
    store = SQLiteStore(tmp_path / "team.db")
    original_error = StoreError("invalid_parameter")
    with pytest.raises(StoreError) as caught:
        async with store.transaction():
            raise original_error
    assert caught.value is original_error


async def test_read_missing_database_does_not_create_it(tmp_path):
    path = tmp_path / "missing.db"
    store = SQLiteStore(path)
    with pytest.raises(StoreError):
        await store.read("SELECT 1")
    assert not path.exists()


@pytest.mark.parametrize("kind", ["sqlite", "http"])
async def test_read_cannot_disable_next_reads_write_protection(tmp_path, kind):
    path = tmp_path / "team.db"
    prepare_leads(path)
    store = make_store(kind, path)
    if kind == "sqlite":
        await store.read("PRAGMA query_only = OFF")
    else:
        with pytest.raises(StoreError):
            await store.read("PRAGMA query_only = OFF")
    with pytest.raises(StoreError):
        await store.read("UPDATE leads SET revision = revision + 1 WHERE id = 1")
    assert (await store.read("SELECT revision FROM leads")).rows == [{"revision": 1}]


async def test_read_connection_uses_configured_busy_timeout(tmp_path):
    path = tmp_path / "team.db"
    prepare_leads(path)
    store = SQLiteStore(path, transaction_timeout=1.25)
    assert (await store.read("PRAGMA busy_timeout")).rows == [{"timeout": 1250}]


@pytest.mark.parametrize("configured, returned", [
    ("https://team.example", "https://Team.Example"),
    ("https://team.example", "https://team.example:443"),
    ("libsql://Team.Example:443", "https://team.example"),
])
async def test_equivalent_server_origin_is_accepted(tmp_path, configured, returned):
    server = PipelineServer(tmp_path / "team.db")
    server.base_url = returned
    store = TursoStore(configured, "private-token", read_auth_token="readonly-token",
                       transport=httpx.MockTransport(server.handle))
    try:
        async with store.transaction() as tx:
            await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
        assert (await store.read("SELECT * FROM leads")).rows == []
    finally:
        for connection in server.streams.values():
            connection.close()
