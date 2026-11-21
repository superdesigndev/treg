"""Running behind a transaction-mode connection pooler: unnamed statements, direct migrations."""

import importlib.util

import pytest
import sqlalchemy.ext.asyncio as sa_asyncio
from sqlalchemy import text

from treg import config, maintenance
from treg.infra import db as infra_db

_PG = "postgresql+asyncpg://app:example@pooler.invalid/treg"


@pytest.fixture
async def load_database(monkeypatch):
    """Isolated module instances of `infra.db`, recording every engine's keyword arguments."""
    modules, created = [], []
    real = sa_asyncio.create_async_engine

    def recording(url, **kwargs):
        created.append((str(url), kwargs))
        return real(url, **kwargs)

    def load(*, primary_url=_PG, read_url="", statement_cache=True):
        settings = config.Settings(_env_file=None, database_url=primary_url,
                                   read_database_url=read_url, db_statement_cache=statement_cache)
        created.clear()
        spec = importlib.util.spec_from_file_location("treg.infra._pooler_test", infra_db.__file__)
        module = importlib.util.module_from_spec(spec)
        with monkeypatch.context() as patch:
            patch.setattr(config, "get_settings", lambda: settings)
            patch.setattr(sa_asyncio, "create_async_engine", recording)
            spec.loader.exec_module(module)
        modules.append(module)
        return module, list(created)

    yield load
    for module in modules:
        await module.dispose_engine()


def test_statement_caches_stay_on_by_default(load_database):
    _, created = load_database(read_url="postgresql://reader:example@replica.invalid/treg")
    for _url, kwargs in created:
        args = kwargs.get("connect_args", {})
        assert "statement_cache_size" not in args
        assert "prepared_statement_cache_size" not in args


def test_turning_the_cache_off_reaches_every_postgres_engine(load_database):
    _, created = load_database(read_url="postgresql://reader:example@replica.invalid/treg",
                               statement_cache=False)
    assert len(created) == 4  # api, admin, background, read
    for _url, kwargs in created:
        assert kwargs["connect_args"]["statement_cache_size"] == 0
        assert kwargs["connect_args"]["prepared_statement_cache_size"] == 0
    read_args = created[-1][1]["connect_args"]
    assert read_args["server_settings"] == {"default_transaction_read_only": "on"}


def test_sqlite_ignores_the_switch(load_database):
    _, created = load_database(primary_url="sqlite+aiosqlite:///./pooler-test.db",
                               statement_cache=False)
    assert all("connect_args" not in kwargs for _url, kwargs in created)


@pytest.mark.skipif(infra_db._is_sqlite, reason="needs PostgreSQL (TREG_TEST_DB_URL)")
@pytest.mark.parametrize("statement_cache", [True, False])
async def test_without_the_cache_statements_are_unnamed(load_database, statement_cache):
    module, _ = load_database(primary_url=infra_db._db_url, statement_cache=statement_cache)
    async with module._engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar() == 1
        assert (await connection.execute(text("SELECT 1"))).scalar() == 1
        named = (await connection.execute(
            text("SELECT count(*) FROM pg_prepared_statements"))).scalar()
    assert (named > 0) is statement_cache


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_migration_url_normalizes_the_async_driver(scheme):
    settings = config.Settings(_env_file=None,
                               migration_database_url=f"{scheme}://owner:example@db.invalid/treg")
    assert settings.migration_database_url == "postgresql+asyncpg://owner:example@db.invalid/treg"


def test_upgrades_use_the_migration_url_when_set(monkeypatch):
    direct = "postgresql+asyncpg://owner:p%40ss@db.invalid/treg"
    settings = config.Settings(_env_file=None, migration_database_url=direct)
    monkeypatch.setattr(maintenance, "get_settings", lambda: settings)
    assert maintenance._alembic_config().get_main_option("sqlalchemy.url") == direct


def test_upgrades_fall_back_to_the_application_url(monkeypatch):
    settings = config.Settings(_env_file=None, migration_database_url="")
    monkeypatch.setattr(maintenance, "get_settings", lambda: settings)
    assert maintenance._alembic_config().get_main_option("sqlalchemy.url") == infra_db._db_url
