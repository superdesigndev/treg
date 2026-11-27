"""The background pool can reach the database through a transaction-pooling PgBouncer."""

import ast
import logging
import os
import pathlib
import re

import asyncpg
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from tests.conftest import ensure_postgres_database
from treg import config
from treg.infra import db as infra_db
from treg.infra import money_trace

DIRECT = "postgresql+asyncpg://app:example@primary.invalid:5432/treg"
POOLER = "postgresql+asyncpg://app:example@primary.invalid:6432/treg"
SRC = pathlib.Path(infra_db.__file__).parent.parent


def _port(engine) -> int | None:
    return make_url(engine.url).port


async def test_the_background_pool_takes_its_own_url_and_the_rest_stay_direct(load_db_module, caplog):
    module = load_db_module(database_url=DIRECT, background_database_url=POOLER,
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
    assert "db pools per process: api 5+10, admin 3+0 = 18;" in caplog.text
    assert "background 10+0 connects through TREG_BACKGROUND_DATABASE_URL, outside these totals" \
        in caplog.text


async def test_without_a_background_url_every_pool_is_direct_and_counted(load_db_module, caplog):
    module = load_db_module(database_url=DIRECT, background_database_url="")
    assert {_port(e) for e in (module._engine, module._admin_engine, module._background_engine)} == {5432}
    assert module._background_engine.sync_engine not in money_trace._pooled_engines

    with caplog.at_level(logging.INFO, logger="treg"):
        module.log_connection_budget()
    assert "db pools per process: api 5+10, admin 3+0, background 10+0 = 28;" in caplog.text
    assert "TREG_BACKGROUND_DATABASE_URL" not in caplog.text


async def test_the_budget_counts_only_what_connects_directly(load_db_module):
    overrides = "api.pool_size=4,api.max_overflow=8,admin.pool_size=1,background.pool_size=5"
    direct = load_db_module(database_url=DIRECT, background_database_url="",
                            db_pool_overrides=overrides).connection_budget(workers=8)
    pooled = load_db_module(database_url=DIRECT, background_database_url=POOLER,
                            db_pool_overrides=overrides).connection_budget(workers=8)
    assert (direct["per_process"], direct["retained_per_process"]) == (18, 10)
    assert (pooled["per_process"], pooled["retained_per_process"]) == (13, 5)
    assert pooled["retained_per_instance"] == 40


@pytest.mark.parametrize("database_url", [DIRECT, "postgresql+psycopg://app:example@primary.invalid/treg"])
@pytest.mark.parametrize("background_url", ["sqlite+aiosqlite:///treg.db", "mysql://app@primary.invalid/treg"])
def test_a_postgres_database_needs_a_postgres_background_url(database_url, background_url):
    with pytest.raises(ValidationError, match="background_database_url must be a PostgreSQL URL"):
        config.Settings(_env_file=None, database_url=database_url,
                        background_database_url=background_url)


def test_the_background_url_gets_the_async_driver():
    settings = config.Settings(_env_file=None, database_url=DIRECT,
                               background_database_url="postgres://app:example@primary.invalid:6432/treg")
    assert settings.background_database_url == POOLER


async def test_sqlite_has_no_pools_to_route(load_db_module, tmp_path):
    module = load_db_module(database_url=f"sqlite+aiosqlite:///{tmp_path / 'treg.db'}",
                            background_database_url=POOLER)
    assert module._background_engine is module._engine
    assert not module._background_url
    await module.verify_background_database()


async def test_startup_checks_the_background_url_before_the_schema(monkeypatch):
    class Checked(Exception):
        pass

    async def checked():
        raise Checked

    monkeypatch.setattr(config.get_settings(), "secret_key", "configured")
    monkeypatch.setattr(infra_db, "verify_background_database", checked)
    with pytest.raises(Checked):
        await infra_db.verify_db()


@pytest.fixture
def postgres_url():
    if not infra_db._db_url.startswith("postgresql"):
        pytest.skip("requires PostgreSQL via TREG_TEST_DB_URL")
    return infra_db._db_url


async def _admin(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(make_url(url).set(drivername="postgresql")
                                 .render_as_string(hide_password=False))


@pytest.fixture
async def other_database(postgres_url):
    name = f"{make_url(postgres_url).database}_background_other"
    url = await ensure_postgres_database(postgres_url, name)
    yield url
    connection = await _admin(postgres_url)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await connection.close()


@pytest.fixture
async def other_role(postgres_url):
    name = f"treg_background_other_{os.getpid()}"
    connection = await _admin(postgres_url)
    try:
        await connection.execute(f"CREATE ROLE {name} LOGIN PASSWORD 'other'")
        await connection.execute(f'GRANT CONNECT ON DATABASE "{make_url(postgres_url).database}" TO {name}')
    finally:
        await connection.close()
    yield make_url(postgres_url).set(username=name, password="other").render_as_string(hide_password=False)
    connection = await _admin(postgres_url)
    try:
        await connection.execute(f'REVOKE CONNECT ON DATABASE "{make_url(postgres_url).database}" FROM {name}')
        await connection.execute(f"DROP ROLE IF EXISTS {name}")
    finally:
        await connection.close()


async def test_a_background_url_on_another_database_refuses_to_start(
        load_db_module, postgres_url, other_database):
    module = load_db_module(database_url=postgres_url, background_database_url=other_database)
    with pytest.raises(RuntimeError, match=f"database '{make_url(other_database).database}' instead of"):
        await module.verify_background_database()
    await module.dispose_engine()


async def test_a_background_url_logging_in_as_another_role_refuses_to_start(
        load_db_module, postgres_url, other_role):
    module = load_db_module(database_url=postgres_url, background_database_url=other_role)
    with pytest.raises(RuntimeError, match=f"role '{make_url(other_role).username}' instead of"):
        await module.verify_background_database()
    await module.dispose_engine()


async def test_a_background_url_that_passes_prepared_statements_through_refuses_to_start(
        load_db_module, postgres_url):
    module = load_db_module(database_url=postgres_url, background_database_url=postgres_url)
    with pytest.raises(RuntimeError, match="passes asyncpg's prepared statements"):
        await module.verify_background_database()


async def test_a_transaction_pooling_pgbouncer_passes_startup():
    if not infra_db._background_url:
        pytest.skip("requires a PgBouncer via TREG_TEST_POOLER_URL")
    await infra_db.verify_background_database()


# Session-level state does not survive a transaction behind a transaction-pooling PgBouncer. Only
# these places may hold it: migrations run on TREG_DATABASE_URL, and a job's run lock
# (`only_one_run`) is taken on its own connection to the API URL, which never goes through a pooler.
SESSION_STATE_ALLOWED = {("alembic/", None), ("infra/db.py", "only_one_run")}
_SESSION_SQL = (
    re.compile(r"(^|;)\s*SET\s+(ROLE\b|SESSION\b|[a-z_][a-z0-9_.]*\s*(=|TO\b))", re.MULTILINE),
    re.compile(r"\bLISTEN\s+\w|\bWITH\s+HOLD\b"),
    re.compile(r"\bpg_(try_)?advisory_lock(_shared)?\s*\(|\bpg_advisory_unlock|"
               r"\bCREATE\s+(TEMP|TEMPORARY)\s+TABLE\b|\bset_config\s*\([^)]*,\s*false\s*\)", re.IGNORECASE),
)
_SESSION_FUNCS = {"pg_advisory_lock", "pg_try_advisory_lock", "pg_advisory_lock_shared",
                  "pg_try_advisory_lock_shared", "pg_advisory_unlock", "pg_advisory_unlock_all"}


def _docstrings(tree: ast.AST) -> set[int]:
    owners = [tree, *(n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))]
    return {id(owner.body[0].value) for owner in owners
            if owner.body and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)}


def _visit(node, function, relative, docstrings, sites) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = node.name
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
        if any(pattern.search(node.value) for pattern in _SESSION_SQL):
            sites.add((relative, function, node.lineno))
    if isinstance(node, ast.JoinedStr):
        joined = "".join(part.value if isinstance(part, ast.Constant) else "x" for part in node.values)
        if any(pattern.search(joined) for pattern in _SESSION_SQL):
            sites.add((relative, function, node.lineno))
    if isinstance(node, ast.Attribute) and node.attr in _SESSION_FUNCS:
        sites.add((relative, function, node.lineno))
    for child in ast.iter_child_nodes(node):
        _visit(child, function, relative, docstrings, sites)


def _session_state_sites() -> set[tuple[str, str | None, int]]:
    sites: set[tuple[str, str | None, int]] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        _visit(tree, None, path.relative_to(SRC).as_posix(), _docstrings(tree), sites)
    return sites


def _allowed(path: str, function: str | None) -> bool:
    return any(path.startswith(prefix) and allowed in (None, function)
               for prefix, allowed in SESSION_STATE_ALLOWED)


def test_session_level_state_stays_off_the_pools_a_pooler_may_carry():
    sites = _session_state_sites()
    assert ("infra/db.py", "only_one_run") in {(p, f) for p, f, _ in sites}, \
        "the scan no longer recognizes the job run lock"
    stray = sorted(site for site in sites if not _allowed(site[0], site[1]))
    assert not stray, f"session-level PostgreSQL state outside {SESSION_STATE_ALLOWED}: {stray}"


@pytest.mark.parametrize("source", [
    'text("BEGIN; SET statement_timeout = 0")',
    '_SQL = "SET search_path TO other"',
    'sa.func.pg_try_advisory_lock(1)',
    'text(f"SET lock_timeout = {ms}")',
    'text("LISTEN changes")',
])
def test_the_session_state_scan_sees_each_form(tmp_path, monkeypatch, source):
    (tmp_path / "application").mkdir()
    (tmp_path / "application" / "writer.py").write_text(f"def write():\n    {source}\n")
    monkeypatch.setattr(f"{__name__}.SRC", tmp_path)
    assert _session_state_sites() == {("application/writer.py", "write", 2)}


@pytest.mark.parametrize("source", [
    'text("SET LOCAL statement_timeout = 5000")',
    'func.pg_advisory_xact_lock(1)',
    '"""Set the key to the value; listen to the caller."""',
    'print(f"  {name:<16} SET BY")',
    'Field(default=None, ondelete="SET NULL")',
])
def test_the_session_state_scan_leaves_transaction_scoped_forms(tmp_path, monkeypatch, source):
    (tmp_path / "application").mkdir()
    (tmp_path / "application" / "writer.py").write_text(f"def write():\n    {source}\n")
    monkeypatch.setattr(f"{__name__}.SRC", tmp_path)
    assert _session_state_sites() == set()
