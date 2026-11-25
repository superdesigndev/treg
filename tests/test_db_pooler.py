"""The background pool can connect through a transaction-pooling PgBouncer; nothing else does."""

import importlib.util
import logging

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from treg import config
from treg.infra import db as infra_db

DIRECT = "postgresql+asyncpg://app:example@primary.invalid:5432/treg"
POOLER = "postgresql+asyncpg://app:example@primary.invalid:6432/treg"


@pytest.fixture
async def load_database(monkeypatch):
    modules = []

    def load(**settings):
        values = config.Settings(_env_file=None, **settings)
        spec = importlib.util.spec_from_file_location("treg.infra._pooler_test", infra_db.__file__)
        module = importlib.util.module_from_spec(spec)
        with monkeypatch.context() as patch:
            patch.setattr(config, "get_settings", lambda: values)
            spec.loader.exec_module(module)
        modules.append(module)
        return module

    yield load
    for module in modules:
        await module.dispose_engine()


def _port(engine) -> int | None:
    return make_url(engine.url).port


async def test_listed_pool_connects_through_the_pooler_and_the_rest_stay_direct(load_database, caplog):
    module = load_database(database_url=DIRECT, database_pooler_url=POOLER,
                           db_pooler_pools="background",
                           read_database_url="postgresql+asyncpg://r:example@replica.invalid/treg")
    assert _port(module._background_engine) == 6432
    assert _port(module._engine) == 5432
    assert _port(module._admin_engine) == 5432
    assert make_url(module._read_engine.url).host == "replica.invalid"
    assert module._db_url == DIRECT, "migrations read `_db_url`, which must stay the direct URL"

    with caplog.at_level(logging.INFO, logger="treg"):
        module.log_connection_budget()
    assert "through the pooler: background" in caplog.text


async def test_no_pooler_keeps_every_pool_direct(load_database, caplog):
    module = load_database(database_url=DIRECT, database_pooler_url=POOLER, db_pooler_pools="")
    assert {_port(e) for e in (module._engine, module._admin_engine, module._background_engine)} == {5432}

    with caplog.at_level(logging.INFO, logger="treg"):
        module.log_connection_budget()
    assert "through the pooler" not in caplog.text


@pytest.mark.parametrize("pools", ["api", "admin", "read", "background,api", "backgroud"])
def test_only_the_background_pool_may_use_the_pooler(pools):
    with pytest.raises(ValidationError, match="db_pooler_pools accepts only background"):
        config.Settings(_env_file=None, database_url=DIRECT, database_pooler_url=POOLER,
                        db_pooler_pools=pools)


def test_pool_list_is_normalized_and_the_pooler_url_gets_the_async_driver():
    settings = config.Settings(_env_file=None, database_url=DIRECT,
                               database_pooler_url="postgres://app:example@primary.invalid:6432/treg",
                               db_pooler_pools=" background, background ,")
    assert settings.db_pooler_pools == "background"
    assert settings.database_pooler_url == POOLER


@pytest.mark.parametrize("pooler_url", ["", "sqlite+aiosqlite:///treg.db"])
def test_listed_pools_on_postgres_need_a_postgres_pooler(pooler_url):
    with pytest.raises(ValidationError, match="requires a PostgreSQL database_pooler_url"):
        config.Settings(_env_file=None, database_url=DIRECT, database_pooler_url=pooler_url,
                        db_pooler_pools="background")


async def test_sqlite_has_no_pools_to_route(load_database, tmp_path):
    module = load_database(database_url=f"sqlite+aiosqlite:///{tmp_path / 'treg.db'}",
                           database_pooler_url="", db_pooler_pools="background")
    assert module._background_engine is module._engine
    assert not module._pooled_pools
