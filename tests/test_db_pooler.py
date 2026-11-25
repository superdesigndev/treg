"""The background pool can connect through a transaction-pooling PgBouncer; nothing else does."""

import ast
import logging
import pathlib
import re

import asyncpg
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from treg import config
from treg.infra import db as infra_db
from treg.infra import money_trace

DIRECT = "postgresql+asyncpg://app:example@primary.invalid:5432/treg"
POOLER = "postgresql+asyncpg://app:example@primary.invalid:6432/treg"
SRC = pathlib.Path(infra_db.__file__).parent.parent


def _port(engine) -> int | None:
    return make_url(engine.url).port


async def test_listed_pool_connects_through_the_pooler_and_the_rest_stay_direct(load_db_module, caplog):
    module = load_db_module(database_url=DIRECT, database_pooler_url=POOLER,
                            db_pooler_pools="background",
                            read_database_url="postgresql+asyncpg://r:example@replica.invalid/treg")
    assert _port(module._background_engine) == 6432
    assert _port(module._engine) == 5432
    assert _port(module._admin_engine) == 5432
    assert make_url(module._read_engine.url).host == "replica.invalid"
    assert module._db_url == DIRECT, "migrations read `_db_url`, which must stay the direct URL"

    assert module._background_engine.sync_engine in money_trace._pooled_engines
    assert module._engine.sync_engine not in money_trace._pooled_engines
    assert module._admin_engine.sync_engine not in money_trace._pooled_engines

    with caplog.at_level(logging.INFO, logger="treg"):
        module.log_connection_budget()
    assert "through the pooler, not in these totals: background" in caplog.text


async def test_the_budget_counts_only_what_connects_directly(load_db_module):
    overrides = "api.pool_size=4,api.max_overflow=8,admin.pool_size=1,background.pool_size=5"
    direct = load_db_module(database_url=DIRECT, database_pooler_url=POOLER, db_pooler_pools="",
                            db_pool_overrides=overrides).connection_budget(workers=8)
    pooled = load_db_module(database_url=DIRECT, database_pooler_url=POOLER,
                            db_pooler_pools="background",
                            db_pool_overrides=overrides).connection_budget(workers=8)
    assert (direct["per_process"], direct["retained_per_process"]) == (18, 10)
    assert (pooled["per_process"], pooled["retained_per_process"]) == (13, 5)
    assert pooled["retained_per_instance"] == 40


async def test_no_pooler_keeps_every_pool_direct(load_db_module, caplog):
    module = load_db_module(database_url=DIRECT, database_pooler_url=POOLER, db_pooler_pools="")
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


@pytest.mark.parametrize("database_url", [DIRECT, "postgresql+psycopg://app:example@primary.invalid/treg"])
@pytest.mark.parametrize("pooler_url", ["", "sqlite+aiosqlite:///treg.db"])
def test_listed_pools_on_postgres_need_a_postgres_pooler(database_url, pooler_url):
    with pytest.raises(ValidationError, match="requires a PostgreSQL database_pooler_url"):
        config.Settings(_env_file=None, database_url=database_url, database_pooler_url=pooler_url,
                        db_pooler_pools="background")


async def test_sqlite_has_no_pools_to_route(load_db_module, tmp_path):
    module = load_db_module(database_url=f"sqlite+aiosqlite:///{tmp_path / 'treg.db'}",
                            database_pooler_url="", db_pooler_pools="background")
    assert module._background_engine is module._engine
    assert not module._pooled_pools
    await module.verify_pooler()


async def test_startup_checks_the_pooler_before_the_schema(monkeypatch):
    class Checked(Exception):
        pass

    async def pooler_checked():
        raise Checked

    monkeypatch.setattr(config.get_settings(), "secret_key", "configured")
    monkeypatch.setattr(infra_db, "verify_pooler", pooler_checked)
    with pytest.raises(Checked):
        await infra_db.verify_db()


@pytest.fixture
def postgres_url():
    if not infra_db._db_url.startswith("postgresql"):
        pytest.skip("requires PostgreSQL via TREG_TEST_DB_URL")
    return infra_db._db_url


async def test_a_pooler_on_the_same_database_passes_startup(load_db_module, postgres_url):
    module = load_db_module(database_url=postgres_url, database_pooler_url=postgres_url,
                            db_pooler_pools="background")
    await module.verify_pooler()


async def test_a_pooler_on_another_database_refuses_to_start(load_db_module, postgres_url):
    url = make_url(postgres_url)
    other = f"{url.database}_pooler_other"
    connection = await asyncpg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False))
    try:
        if not await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", other):
            await connection.execute(f'CREATE DATABASE "{other}"')
    finally:
        await connection.close()
    module = load_db_module(database_url=postgres_url, db_pooler_pools="background",
                            database_pooler_url=url.set(database=other).render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match=f"reaches database '{other}'"):
        await module.verify_pooler()


# Session-level state does not survive a transaction behind a transaction-pooling PgBouncer. These
# files may hold it: migrations run on TREG_DATABASE_URL, and the archive retention run lock is
# taken on the API pool, which never goes through the pooler.
SESSION_STATE_ALLOWED = ("alembic/", "application/archive_retention.py")
_SESSION_SQL = re.compile(
    r"^\s*SET\s+(?!LOCAL\b)|\bpg_(try_)?advisory_lock(_shared)?\s*\(|\bpg_advisory_unlock|\bLISTEN\b"
    r"|\bCREATE\s+(TEMP|TEMPORARY)\b|\bWITH\s+HOLD\b|set_config\s*\([^)]*,\s*false\s*\)",
    re.IGNORECASE)
_SESSION_FUNCS = {"pg_advisory_lock", "pg_try_advisory_lock", "pg_advisory_lock_shared",
                  "pg_try_advisory_lock_shared", "pg_advisory_unlock", "pg_advisory_unlock_all"}


def _session_state_sites() -> set[tuple[str, int]]:
    sites = set()
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in ("text", "execute", "exec_driver_sql") and node.args:
                sql = node.args[0]
                if isinstance(sql, ast.JoinedStr):
                    sql = "".join(part.value if isinstance(part, ast.Constant) else "x"
                                  for part in sql.values)
                elif isinstance(sql, ast.Constant) and isinstance(sql.value, str):
                    sql = sql.value
                else:
                    continue
                if _SESSION_SQL.search(sql):
                    sites.add((relative, node.lineno))
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == "func" and func.attr in _SESSION_FUNCS):
                sites.add((relative, node.lineno))
    return sites


def test_session_level_state_stays_off_the_pools_a_pooler_may_carry():
    sites = _session_state_sites()
    assert any(path == "application/archive_retention.py" for path, _ in sites), \
        "the scan no longer recognizes the retention run lock"
    stray = sorted(site for site in sites if not site[0].startswith(SESSION_STATE_ALLOWED))
    assert not stray, f"session-level PostgreSQL state outside {SESSION_STATE_ALLOWED}: {stray}"
