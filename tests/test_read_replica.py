"""Replica routing is opt-in; its pool must not become a second write path."""

import importlib.util
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from treg import config
from treg.infra import db as infra_db


@pytest.fixture
async def load_database(monkeypatch):
    """Build isolated module instances without replacing the application's imported makers."""
    modules = []

    def load(read_url="", overrides="", *, primary_url=None):
        settings = config.Settings(_env_file=None, database_url=primary_url or infra_db._db_url,
                                   read_database_url=read_url, db_pool_overrides=overrides)
        spec = importlib.util.spec_from_file_location("treg.infra._replica_test", infra_db.__file__)
        module = importlib.util.module_from_spec(spec)
        with monkeypatch.context() as patch:
            patch.setattr(config, "get_settings", lambda: settings)
            spec.loader.exec_module(module)
        modules.append(module)
        return module

    yield load
    for module in modules:
        await module.dispose_engine()


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_replica_url_normalizes_the_async_driver(monkeypatch, scheme):
    monkeypatch.setenv("TREG_READ_DATABASE_URL", f"{scheme}://reader:example@replica.invalid/treg")
    settings = config.Settings(_env_file=None)
    assert settings.read_database_url == "postgresql+asyncpg://reader:example@replica.invalid/treg"


@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:///replica.db",
    "sqlite+aiosqlite:///:memory:",
    "sqlite+aiosqlite:///file:replica.db?mode=ro&uri=true",
])
def test_sqlite_read_urls_are_preserved(url):
    assert config.Settings(_env_file=None, read_database_url=url).read_database_url == url


@pytest.mark.parametrize("url", [
    "mysql://replica/treg", "sqlite:///replica.db", "postgresql+psycopg://replica/treg", "replica",
])
def test_read_datasource_rejects_unsupported_drivers(url):
    with pytest.raises(ValidationError, match="must use sqlite\\+aiosqlite or postgresql\\+asyncpg"):
        config.Settings(_env_file=None, read_database_url=url)


def test_unconfigured_reads_reuse_the_primary_without_an_extra_pool(load_database):
    db = load_database()
    assert db.read_session_maker is db.session_maker
    assert db._read_engine is db._engine
    assert "read" not in db.pool_snapshot()
    assert len(set(db._engines)) == (1 if db._is_sqlite else 3)


async def test_configured_reads_have_their_own_pool_and_budget(load_database):
    # A username containing "sqlite" must not disable the PostgreSQL pool bounds.
    db = load_database("postgresql://sqlite-reader:example@replica.invalid/treg", "read.pool_size=4")
    async with db.read_session_maker() as reader, db.session_maker() as writer:
        assert reader.bind is db._read_engine
        assert writer.bind is db._engine
        assert reader.bind is not writer.bind
        assert reader.bind.url.host == "replica.invalid"
    assert db.pool_snapshot()["read"] == {"checked_out": 0, "capacity": 4}
    assert db._read_engine.pool._max_overflow == 0
    assert db.connection_budget(workers=2) == load_database().connection_budget(workers=2)


async def test_replica_connection_failure_never_falls_back_to_primary(load_database):
    db = load_database("postgresql://reader:example@replica.invalid/treg")

    @event.listens_for(db._read_engine.sync_engine, "do_connect")
    def fail_replica(dialect, connection_record, args, kwargs):
        assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
        raise RuntimeError("replica unavailable")

    @event.listens_for(db._engine.sync_engine, "do_connect")
    def forbid_primary(*args):
        pytest.fail("replica failure opened a primary connection")

    with pytest.raises(RuntimeError, match="replica unavailable"):
        async with db.read_session_maker() as reader:
            await reader.execute(text("SELECT 1"))


@pytest.mark.parametrize("read_url", [
    "", "postgresql://reader:example@replica.invalid/treg", "sqlite+aiosqlite:///:memory:",
])
async def test_shutdown_disposes_each_pool_once(load_database, read_url):
    db = load_database(read_url)
    disposed = []
    for engine in set(db._engines):
        event.listen(engine.sync_engine, "engine_disposed", disposed.append)
    await db.dispose_engine()
    assert set(disposed) == {engine.sync_engine for engine in db._engines}
    assert len(disposed) == len(set(db._engines))
    assert disposed.count(db._read_engine.sync_engine) == 1


@pytest.fixture
def sqlite_sources(tmp_path):
    urls = []
    for name, value in (("primary", 1), ("replica", 9)):
        path = tmp_path / f"{name}.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE read_replica_probe (id integer PRIMARY KEY)")
            connection.execute("INSERT INTO read_replica_probe VALUES (?)", (value,))
            connection.commit()
        urls.append(f"sqlite+aiosqlite:///{path}")
    return urls


async def test_unconfigured_sqlite_keeps_primary_writable(load_database, sqlite_sources):
    db = load_database(primary_url=sqlite_sources[0])
    async with db.read_session_maker() as session:
        assert await session.scalar(text("SELECT id FROM read_replica_probe")) == 1
        await session.execute(text("INSERT INTO read_replica_probe VALUES (2)"))
        await session.commit()
    async with db.session_maker() as session:
        assert await session.scalar(text("SELECT count(*) FROM read_replica_probe")) == 2


@pytest.mark.parametrize("same_database", [False, True])
@pytest.mark.parametrize("read_only_file", [False, True])
async def test_sqlite_reader_guards_writes_without_affecting_primary(
    load_database, sqlite_sources, same_database, read_only_file,
):
    primary_url, replica_url = sqlite_sources
    read_url = primary_url if same_database else replica_url
    if read_only_file:
        read_url = read_url.replace("sqlite+aiosqlite:///", "sqlite+aiosqlite:///file:") + "?mode=ro&uri=true"
    db = load_database(read_url, primary_url=primary_url)
    assert db._read_engine is not db._engine
    assert "read" not in db.pool_snapshot()  # SQLite uses the driver's default pool behavior.
    for statement in (
        "INSERT INTO read_replica_probe VALUES (2)",
        "UPDATE read_replica_probe SET id = 2",
        "DELETE FROM read_replica_probe",
        "CREATE TABLE read_replica_forbidden (id integer)",
        "DROP TABLE read_replica_probe",
        "CREATE TEMP TABLE read_replica_temporary (id integer)",
    ):
        async with db.read_session_maker() as reader:
            assert await reader.scalar(text("PRAGMA query_only")) == 1
            assert await reader.scalar(text("SELECT id FROM read_replica_probe")) == (1 if same_database else 9)
            await reader.commit()
            with pytest.raises(DBAPIError) as error:
                await reader.execute(text(statement))
            assert error.value.orig.sqlite_errorcode == sqlite3.SQLITE_READONLY
            await reader.rollback()
            assert await reader.scalar(text("PRAGMA query_only")) == 1
    async with db.session_maker() as writer:
        assert await writer.scalar(text("PRAGMA query_only")) == 0
        await writer.execute(text("INSERT INTO read_replica_probe VALUES (2)"))
        await writer.commit()
    await db.dispose_engine()
    # A newly opened connection must retain the guard after pool disposal/reconnection.
    async with db.read_session_maker() as reader:
        assert await reader.scalar(text("PRAGMA query_only")) == 1
        assert await reader.scalar(text("SELECT count(*) FROM read_replica_probe")) == (2 if same_database else 1)
        with pytest.raises(DBAPIError):
            await reader.execute(text("DELETE FROM read_replica_probe"))


async def test_sqlite_read_only_uri_failure_does_not_create_file_or_use_primary(load_database, tmp_path):
    missing = tmp_path / "missing.db"
    db = load_database(f"sqlite+aiosqlite:///file:{missing}?mode=ro&uri=true")

    @event.listens_for(db._engine.sync_engine, "do_connect")
    def forbid_primary(*args):
        pytest.fail("replica failure opened a primary connection")

    with pytest.raises(DBAPIError, match="unable to open database file"):
        async with db.read_session_maker() as reader:
            await reader.execute(text("SELECT 1"))
    assert not missing.exists()


@pytest.mark.skipif(infra_db._is_sqlite, reason="requires the isolated PostgreSQL test database")
async def test_postgres_replica_reads_but_rejects_writes_across_transactions(load_database):
    # Deliberately point the reader at the writable TEST database: its connection default must
    # reject writes even before an operator provisions a physical replica or a restricted role.
    db = load_database(infra_db._db_url, "read.pool_size=1")
    async with db._engine.begin() as connection:
        await connection.execute(text("CREATE TABLE read_replica_probe (id integer PRIMARY KEY)"))
        await connection.execute(text("INSERT INTO read_replica_probe VALUES (1)"))
    try:
        for statement in (
            "INSERT INTO read_replica_probe VALUES (2)",
            "UPDATE read_replica_probe SET id = 2",
            "DELETE FROM read_replica_probe",
            "CREATE TABLE read_replica_forbidden (id integer)",
            "DROP TABLE read_replica_probe",
        ):
            async with db.read_session_maker() as reader:
                assert await reader.scalar(text("SHOW transaction_read_only")) == "on"
                assert await reader.scalar(text("SELECT id FROM read_replica_probe")) == 1
                await reader.commit()
                with pytest.raises(DBAPIError) as error:
                    await reader.execute(text(statement))
                assert error.value.orig.sqlstate == "25006"  # read_only_sql_transaction
                await reader.rollback()
                assert await reader.scalar(text("SHOW transaction_read_only")) == "on"
        async with db._engine.begin() as connection:
            assert await connection.scalar(text("SHOW transaction_read_only")) == "off"
            await connection.execute(text("INSERT INTO read_replica_probe VALUES (2)"))
        await db.dispose_engine()
        async with db.read_session_maker() as reader:
            assert await reader.scalar(text("SHOW transaction_read_only")) == "on"
            assert await reader.scalar(text("SELECT count(*) FROM read_replica_probe")) == 2
    finally:
        async with db._engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS read_replica_forbidden"))
            await connection.execute(text("DROP TABLE read_replica_probe"))
