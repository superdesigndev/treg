"""Release maintenance runs explicitly before serving, never inside an app lifespan."""

from __future__ import annotations

import asyncio
import functools
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path: Path, *, single_user: bool = False) -> tuple[dict[str, str], Path, Path]:
    database = tmp_path / "upgrade.db"
    token_file = tmp_path / "local-token"
    env = os.environ.copy()
    env.update({
        "TREG_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "TREG_PUBLIC_URL": "http://localhost:18790",
        "TREG_SINGLE_USER": "true" if single_user else "false",
        "TREG_SINGLE_USER_TOKEN_FILE": str(token_file),
        "TREG_CLAUDE_CONNECTOR_ENABLED": "false",
    })
    return env, database, token_file


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, text=True,
        capture_output=True, timeout=90, check=False,
    )


def _upgrade(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(["-m", "treg", "upgrade"], env)


def _alembic_upgrade(env: dict[str, str], revision: str) -> subprocess.CompletedProcess[str]:
    return _run(["-m", "alembic", "upgrade", revision], env)


def _create_unstamped_schema(env: dict[str, str]) -> None:
    script = textwrap.dedent(
        """
        import asyncio

        from sqlmodel import SQLModel

        from treg import models
        from treg.infra.db import _engine, dispose_engine

        async def main():
            async with _engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
                await connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
            await dispose_engine()

        asyncio.run(main())
        """
    )
    result = _run(["-c", script], env)
    assert result.returncode == 0, result.stderr


@functools.cache
def _script_directory():
    from alembic.script import ScriptDirectory

    from treg.maintenance import _alembic_config

    return ScriptDirectory.from_config(_alembic_config())


def _alembic_head() -> str:
    return _script_directory().get_current_head()


def _one_behind_head() -> str:
    return _script_directory().get_revision(_alembic_head()).down_revision


def _alembic_version(database: Path) -> str | None:
    with sqlite3.connect(database) as db:
        version_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone()
        if version_table is None:
            return None
        row = db.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None


def _seed_connection(env: dict[str, str]) -> None:
    script = textwrap.dedent(
        """
        import asyncio

        from sqlalchemy import insert
        from sqlalchemy import inspect as sa_inspect

        from treg import oauth_providers
        from treg.infra.db import session_maker
        from treg.models import Org, Secret, Tool

        async def seed():
            provider = oauth_providers.get("google-analytics")
            assert provider is not None
            async with session_maker() as db:
                # The org table at 0026 predates columns the current model carries.
                present = {c["name"] for c in await db.run_sync(
                    lambda sync: sa_inspect(sync.connection()).get_columns("org"))}
                values = {k: v for k, v in Org(name="Upgrade Test", slug="upgrade-test").model_dump().items()
                          if k in present and v is not None}
                org_id = (await db.execute(insert(Org.__table__).values(**values))).inserted_primary_key[0]
                secret = Secret(
                    org_id=org_id,
                    name="google-analytics",
                    owner="owner@example.test",
                    kind="oauth",
                    value="unused-encrypted-placeholder",
                    provider="google-analytics",
                )
                db.add(secret)
                await db.flush()
                db.add(Tool(
                    org_id=org_id,
                    name="google-analytics",
                    owner="owner@example.test",
                    base_url=provider.base_url,
                    host="analyticsdata.googleapis.com",
                    bindings=[{"secret_id": secret.id}],
                ))
                await db.commit()

        asyncio.run(seed())
        """
    )
    result = _run(["-c", script], env)
    assert result.returncode == 0, result.stderr


def _companion_count(database: Path) -> int:
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT COUNT(*) FROM tool WHERE name = 'google-analytics-admin'"
        ).fetchone()[0]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _boot_raw_asgi(env: dict[str, str], tmp_path: Path) -> tuple[bool, str]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {**env, "PORT": str(port), "TREG_PUBLIC_URL": base_url, "NO_COLOR": "1"}
    # The assertions read the server's log text. Rich honours FORCE_COLOR even into a file and
    # then highlights numbers with escape codes ("Database revision \x1b[1;36m9999"), so a shell
    # that forces colour (agent harnesses do) would fail the revision checks for no reason.
    for forcing in ("FORCE_COLOR", "CLICOLOR_FORCE", "TTY_COMPATIBLE"):
        env.pop(forcing, None)
    stdout_path = tmp_path / "raw-asgi.stdout"
    stderr_path = tmp_path / "raw-asgi.stderr"
    ready = False

    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "treg.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"{base_url}/meta", timeout=1) as response:
                        ready = response.status == 200
                except (OSError, urllib.error.URLError):
                    pass
                if ready:
                    break
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    return ready, stderr_path.read_text()


def test_real_serve_path_can_query_database_after_pre_serve_maintenance(tmp_path):
    env, _, token_file = _env(tmp_path, single_user=True)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env.update({"PORT": str(port), "TREG_PUBLIC_URL": base_url})
    stdout_path = tmp_path / "server.stdout"
    stderr_path = tmp_path / "server.stderr"
    ready = False
    request_status = None
    request_detail = "request was not attempted"

    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "treg"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"{base_url}/meta", timeout=1) as response:
                        ready = response.status == 200
                except (OSError, urllib.error.URLError):
                    pass
                if ready and token_file.exists():
                    break
                time.sleep(0.1)

            if ready and token_file.exists():
                request = urllib.request.Request(
                    f"{base_url}/tools",
                    headers={"X-Treg-Token": token_file.read_text().strip()},
                )
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        request_status = response.status
                        request_detail = response.read().decode(errors="replace")
                except urllib.error.HTTPError as exc:
                    request_status = exc.code
                    request_detail = exc.read().decode(errors="replace")
                except (OSError, urllib.error.URLError) as exc:
                    request_detail = repr(exc)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    server_stderr = stderr_path.read_text()
    assert ready, f"server never became ready\nserver stderr:\n{server_stderr}"
    assert token_file.exists(), f"single-user token was not written\nserver stderr:\n{server_stderr}"
    assert request_status == 200, (
        f"GET /tools returned {request_status}: {request_detail}\n"
        f"server stderr:\n{server_stderr}"
    )


def test_empty_database_upgrade_uses_pure_alembic_without_provisioning_a_user(tmp_path):
    env, database, token_file = _env(tmp_path, single_user=True)

    result = _upgrade(env)

    assert result.returncode == 0, result.stderr
    assert "treg schema: alembic upgrade head (empty database)" in result.stdout
    assert "treg upgrade complete" in result.stdout
    with sqlite3.connect(database) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        users = db.execute('SELECT COUNT(*) FROM "user"').fetchone()[0]
    assert {"org", "secret", "tool"} <= tables
    assert _alembic_version(database) == _alembic_head()
    assert users == 0
    assert not token_file.exists(), "hosted upgrade must never provision the local user"


def test_raw_asgi_boots_when_database_is_at_head(tmp_path):
    env, _, _ = _env(tmp_path)
    result = _upgrade(env)
    assert result.returncode == 0, result.stderr

    ready, server_stderr = _boot_raw_asgi(env, tmp_path)

    assert ready, server_stderr


def test_raw_asgi_refuses_a_database_behind_head(tmp_path):
    env, database, _ = _env(tmp_path)
    previous = _one_behind_head()
    result = _alembic_upgrade(env, previous)
    assert result.returncode == 0, result.stderr
    assert _alembic_version(database) == previous

    ready, server_stderr = _boot_raw_asgi(env, tmp_path)

    assert not ready
    assert "behind this build" in server_stderr
    assert "python -m treg upgrade" in server_stderr


def test_raw_asgi_warns_and_serves_when_database_revision_is_unknown_newer(tmp_path):
    env, database, _ = _env(tmp_path)
    result = _upgrade(env)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as db:
        db.execute("UPDATE alembic_version SET version_num = '9999'")

    ready, server_stderr = _boot_raw_asgi(env, tmp_path)

    assert ready, server_stderr
    # Rich logging wraps the warning and inserts its source location, so pin the semantic pieces.
    assert "Database revision 9999" in server_stderr
    assert "additive revisions" in server_stderr
    assert "contract revision does not" in server_stderr


def test_upgrade_refuses_an_unstamped_pre_adoption_database(tmp_path):
    env, database, _ = _env(tmp_path)
    _create_unstamped_schema(env)
    assert _alembic_version(database) is None

    result = _upgrade(env)

    assert result.returncode != 0
    assert "tools-registry[server]==0.14.*" in result.stderr
    assert "python -m treg upgrade" in result.stderr
    assert "Nothing was changed" in result.stderr
    assert _alembic_version(database) is None


def test_upgrade_names_the_rollback_when_the_database_is_newer_than_the_build(tmp_path):
    """A database stamped at a revision this build does not know is a rollback past the rollback
    floor (or a stale checkout). The operator gets an instruction, not an Alembic stack trace."""
    env, database, _ = _env(tmp_path)
    initial = _upgrade(env)
    assert initial.returncode == 0, initial.stderr
    with sqlite3.connect(database) as db:
        db.execute("UPDATE alembic_version SET version_num = '9999'")

    result = _upgrade(env)

    assert result.returncode != 0
    assert "OLDER than the schema" in result.stderr
    assert "No migration ran" in result.stderr
    assert _alembic_version(database) == "9999"


def test_upgrade_applies_a_pending_revision_from_one_behind_head(tmp_path):
    env, database, _ = _env(tmp_path)
    previous = _one_behind_head()
    assert previous != _alembic_head()
    initial = _alembic_upgrade(env, previous)
    assert initial.returncode == 0, initial.stderr
    assert _alembic_version(database) == previous

    result = _upgrade(env)

    assert result.returncode == 0, result.stderr
    assert "treg schema: alembic upgrade head (stamped database)" in result.stdout
    assert _alembic_version(database) == _alembic_head()


def test_upgrade_backfills_companions_and_is_idempotent(tmp_path):
    env, database, _ = _env(tmp_path)
    initial = _upgrade(env)
    assert initial.returncode == 0, initial.stderr
    _seed_connection(env)
    assert _companion_count(database) == 0

    first = _upgrade(env)
    assert first.returncode == 0, first.stderr
    assert _companion_count(database) == 1

    second = _upgrade(env)
    assert second.returncode == 0, second.stderr
    assert _companion_count(database) == 1


@pytest.mark.parametrize("ads_enabled", [False, True])
def test_app_lifespan_does_not_run_release_backfills(tmp_path, ads_enabled):
    env, database, _ = _env(tmp_path)
    # An empty outbox starts the real worker without making any upstream calls.
    env["TREG_GOOGLE_ADS_CUSTOMER_ID"] = "test-customer" if ads_enabled else ""
    env["TREG_ADS_CONV_REFRESH_TOKEN"] = "test-refresh-token" if ads_enabled else ""
    initial = _upgrade(env)
    assert initial.returncode == 0, initial.stderr
    _seed_connection(env)
    assert _companion_count(database) == 0
    script = textwrap.dedent(
        """
        import asyncio

        from treg.bootstrap import create_app

        async def start_and_stop():
            app = create_app("all")
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(start_and_stop())
        """
    )

    result = _run(["-c", script], env)

    assert result.returncode == 0, result.stderr
    assert _companion_count(database) == 0


# --- lock-timeout retry ---------------------------------------------------------------------
# A hot-table ALTER waits at most `migration_lock_timeout_s` for its lock (alembic/env.py) so that
# queued traffic stalls that long at most; the deploy waits longer by retrying, never by waiting
# longer on one attempt.


class _LockNotAvailableError(Exception):
    """asyncpg's class, by name, so the detection sees what production sees."""


def _lock_timeout() -> Exception:
    from sqlalchemy.exc import DBAPIError

    return DBAPIError(
        "ALTER TABLE callrecord ADD COLUMN api_key_id INTEGER", {},
        _LockNotAvailableError("canceling statement due to lock timeout"),
    )


def _other_database_error() -> Exception:
    from sqlalchemy.exc import DBAPIError

    return DBAPIError("ALTER TABLE callrecord", {}, RuntimeError("relation does not exist"))


def _fake_upgrade(monkeypatch, outcomes: list[Exception | None], *,
                  holders: list[str] | None = None) -> list[str]:
    """`alembic upgrade head` that raises each queued outcome in turn, then succeeds. The calls
    list records "preflight" when the lock-holder check runs, and `holders` makes it refuse."""
    from treg import maintenance
    from treg.config import get_settings

    calls: list[str] = []

    def upgrade(config, revision):
        calls.append(revision)
        outcome = outcomes.pop(0) if outcomes else None
        if outcome is not None:
            raise outcome

    async def stamped():
        return {"alembic_version", "org"}

    async def preflight(config, tables):
        calls.append("preflight")
        if holders:
            raise RuntimeError("Refusing to upgrade: " + "; ".join(holders))

    monkeypatch.setattr(maintenance.command, "upgrade", upgrade)
    monkeypatch.setattr(maintenance, "_table_names", stamped)
    monkeypatch.setattr(maintenance, "_refuse_behind_long_lock_holder", preflight)
    monkeypatch.setattr(get_settings(), "migration_lock_retry_pause_s", 0.0)
    return calls


async def test_upgrade_retries_a_lock_timeout_and_resumes(monkeypatch, caplog):
    from treg import maintenance

    calls = _fake_upgrade(monkeypatch, [_lock_timeout(), _lock_timeout()])

    with caplog.at_level("WARNING", logger="treg.maintenance"):
        await maintenance._upgrade_schema()

    assert calls == ["preflight", "head", "head", "head"]
    retries = [r for r in caplog.records if "migration lock timeout" in r.getMessage()]
    assert [r.getMessage()[:40] for r in retries] == [
        "migration lock timeout on attempt 1/30; ", "migration lock timeout on attempt 2/30; ",
    ]


async def test_upgrade_gives_up_after_the_retry_budget(monkeypatch):
    from sqlalchemy.exc import DBAPIError

    from treg import maintenance

    from treg.config import get_settings

    monkeypatch.setattr(get_settings(), "migration_lock_retry_attempts", 3)
    calls = _fake_upgrade(monkeypatch, [_lock_timeout()] * 5)

    with pytest.raises(DBAPIError, match="lock timeout"):
        await maintenance._upgrade_schema()

    assert calls == ["preflight"] + ["head"] * 3


async def test_upgrade_does_not_retry_other_database_errors(monkeypatch):
    from sqlalchemy.exc import DBAPIError

    from treg import maintenance

    calls = _fake_upgrade(monkeypatch, [_other_database_error()])

    with pytest.raises(DBAPIError, match="does not exist"):
        await maintenance._upgrade_schema()

    assert calls == ["preflight", "head"]


def test_lock_wait_defaults_stay_above_the_deadlock_timeout():
    from pydantic import ValidationError

    from treg.config import Settings

    settings = Settings(_env_file=None)
    assert settings.migration_lock_timeout_s == 2.0
    assert settings.migration_lock_retry_attempts == 30
    with pytest.raises(ValidationError):
        Settings(_env_file=None, migration_lock_timeout_s=1.0)


# --- refusing behind a long lock holder -----------------------------------------------------
# PostgreSQL cancels an ordinary autovacuum that blocks a waiting ALTER, but never an
# anti-wraparound one, and a long transaction keeps its locks until it ends: retrying behind
# either only repeats the stall, so the upgrade refuses before it requests any lock.


async def test_upgrade_refuses_behind_a_long_lock_holder_before_any_migration(monkeypatch):
    from treg import maintenance

    calls = _fake_upgrade(monkeypatch, [], holders=["pid 7 holds AccessShareLock on org"])

    with pytest.raises(RuntimeError, match="pid 7 holds AccessShareLock on org"):
        await maintenance._upgrade_schema()

    assert calls == ["preflight"]


def _activity(**row):
    from types import SimpleNamespace

    base = {"pid": 7, "relname": "ledgerentry", "mode": "ShareUpdateExclusiveLock",
            "backend_type": "autovacuum worker", "query": "", "xact_s": 900.0,
            "past_freeze_age": False}
    return SimpleNamespace(**{**base, **row})


def test_only_an_anti_wraparound_autovacuum_or_a_long_transaction_is_a_long_holder(caplog):
    from treg.maintenance import _long_holder

    wraparound = _activity(query="autovacuum: VACUUM public.ledgerentry (to prevent wraparound)")
    assert "to prevent wraparound" in _long_holder(wraparound)
    assert _long_holder(_activity(query="autovacuum: VACUUM ANALYZE public.ledgerentry")) is None
    transaction = _activity(backend_type="client backend", mode="AccessShareLock", xact_s=45.2)
    assert _long_holder(transaction) == (
        "pid 7 (client backend, transaction open 45 s) holds AccessShareLock on ledgerentry")
    with caplog.at_level("WARNING", logger="treg.maintenance"):
        assert _long_holder(_activity(query="<insufficient privilege>")) is None
    assert "pg_read_all_stats" in caplog.text
    assert "past its freeze age" in _long_holder(
        _activity(query="<insufficient privilege>", past_freeze_age=True))


def _hidden(**row):
    """What pg_stat_activity shows a role without pg_read_all_stats for another role's session,
    autovacuum included: the pid, and NULL kind, start and query."""
    return _activity(backend_type=None, query=None, xact_s=None, **row)


def test_a_session_this_role_cannot_see_is_judged_by_its_lock_and_the_freeze_age(caplog):
    from treg.maintenance import _long_holder

    refused = _long_holder(_hidden(past_freeze_age=True))
    assert refused.startswith("pid 7 (hidden from this role; on a table past its freeze age")
    assert refused.endswith("holds ShareUpdateExclusiveLock on ledgerentry")
    with caplog.at_level("WARNING", logger="treg.maintenance"):
        assert _long_holder(_hidden()) is None
        assert _long_holder(_hidden(mode="AccessShareLock", past_freeze_age=True)) is None
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2
    assert all("cannot see pid 7" in w and "pg_read_all_stats" in w for w in warnings)


_FIXTURE_REVISIONS = {
    "r1": (None, '''"""creates alpha; the prose names beta, which it does not touch"""
from alembic import op


def upgrade():
    op.create_table("alpha")
'''),
    "r2": ("r1", '''"""alters alpha and creates gamma; beta appears only in prose"""
from alembic import op


def upgrade():
    """beta is named here too, in a docstring"""
    # and beta in a comment
    op.execute("ALTER TABLE alpha ADD COLUMN note TEXT")
    op.create_table("gamma")
'''),
}


def _fixture_script(tmp_path: Path):
    """A script directory of its own, so the expectation never depends on main's revisions."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    (tmp_path / "versions").mkdir()
    for revision, (down, body) in _FIXTURE_REVISIONS.items():
        (tmp_path / "versions" / f"{revision}.py").write_text(
            body + f"\n\nrevision = {revision!r}\ndown_revision = {down!r}\n"
            "branch_labels = None\ndepends_on = None\n")
    config = Config()
    config.set_main_option("script_location", str(tmp_path))
    return ScriptDirectory.from_config(config)


def test_pending_tables_are_the_existing_ones_the_pending_revisions_name(tmp_path):
    from treg.maintenance import _tables_named_by_pending

    script = _fixture_script(tmp_path)
    existing = {"alpha", "beta", "alembic_version"}

    assert _tables_named_by_pending(script, (), existing) == {"alpha"}
    assert _tables_named_by_pending(script, ("r1",), existing) == {"alpha"}
    assert _tables_named_by_pending(script, ("r1",), {"beta", "gamma"}) == {"gamma"}
    assert _tables_named_by_pending(script, ("r2",), existing) == set()
    assert _tables_named_by_pending(script, ("unknown",), existing) == set()


async def test_postgres_preflight_refuses_a_transaction_holding_a_pending_table(monkeypatch):
    from sqlalchemy import text

    from treg import maintenance
    from treg.config import get_settings
    from treg.infra.db import _engine, reset_db

    if _engine.dialect.name != "postgresql":
        pytest.skip("pg_locks and pg_stat_activity are PostgreSQL's")
    await reset_db()

    async def pending(config, tables):
        return {"org"}

    monkeypatch.setattr(maintenance, "_pending_revision_tables", pending)
    monkeypatch.setattr(get_settings(), "migration_lock_holder_max_xact_s", 0.5)
    config = maintenance._alembic_config()
    async with _engine.connect() as holder:
        await holder.execute(text("LOCK TABLE org IN ACCESS SHARE MODE"))
        await maintenance._refuse_behind_long_lock_holder(config, {"org"})  # still young
        await asyncio.sleep(0.8)
        with pytest.raises(RuntimeError, match=r"transaction open \d+ s\) holds AccessShareLock on org"):
            await maintenance._refuse_behind_long_lock_holder(config, {"org"})
        monkeypatch.setattr(get_settings(), "migration_lock_holder_check", False)
        await maintenance._refuse_behind_long_lock_holder(config, {"org"})
        await holder.rollback()


async def test_postgres_preflight_sees_another_roles_lock_without_pg_read_all_stats(caplog):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from treg.infra.db import _engine, reset_db
    from treg.maintenance import _LONG_LOCK_HOLDERS, _long_holder

    if _engine.dialect.name != "postgresql":
        pytest.skip("pg_locks and pg_stat_activity are PostgreSQL's")
    await reset_db()
    try:
        async with _engine.begin() as admin:
            await admin.execute(text(
                "DO $$ BEGIN CREATE ROLE treg_lockguard_probe NOLOGIN; "
                "EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $$"))
    except DBAPIError:
        pytest.skip("the test role cannot create a role")
    async with _engine.connect() as holder, _engine.connect() as checker:
        await holder.execute(text("LOCK TABLE org IN SHARE UPDATE EXCLUSIVE MODE"))
        await checker.execute(text("SET ROLE treg_lockguard_probe"))
        rows = (await checker.execute(_LONG_LOCK_HOLDERS, {
            "tables": ["org"], "max_xact_s": 3600})).all()
        await checker.execute(text("RESET ROLE"))
        await holder.rollback()
    assert [(r.relname, r.mode, r.backend_type, r.xact_s) for r in rows] == [
        ("org", "ShareUpdateExclusiveLock", None, None)]
    with caplog.at_level("WARNING", logger="treg.maintenance"):
        assert (_long_holder(rows[0]) is None) is (not rows[0].past_freeze_age)
    if not rows[0].past_freeze_age:
        assert "pg_read_all_stats" in caplog.text
