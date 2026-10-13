"""The blocklist comes from Postgres only: a version poll, an in-process cache, a bounded cold start."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from treg import analytics
from treg.application.ratelimit import background, blocklist_state, blocklist_sync, limiter
from treg.config import get_settings
from treg.infra import db as infra_db
from treg.infra.db import reset_db, session_maker
from treg.models import BlocklistEntry, BlocklistVersion
from treg.timeutil import utcnow_naive


@pytest.fixture(autouse=True)
def _fresh():
    get_settings.cache_clear()
    limiter.reset_for_tests()
    background.reset_for_tests()
    yield
    limiter.reset_for_tests()
    background.reset_for_tests()
    get_settings.cache_clear()


@pytest.fixture
def alerts(monkeypatch):
    events: list = []
    monkeypatch.setattr(analytics, "capture", lambda _id, e, p=None, **_: events.append((e, p)))
    return events


async def _write(version: int, *orgs: str, revoked: bool = False) -> None:
    async with session_maker() as db:
        now = utcnow_naive()
        for org in orgs:
            db.add(BlocklistEntry(kind="org_id", value=org, reason="r", approved_by="o", evidence={"q": 1},
                                  created_by="t", created_at=now, expires_at=now + timedelta(hours=1),
                                  revoked_at=now if revoked else None))
        row = await db.get(BlocklistVersion, 1)
        row.version = version
        await db.commit()


def _statements():
    seen: list[str] = []

    def before(conn, cursor, statement, *_):
        seen.append(statement)

    event.listen(infra_db._ratelimit_engine.sync_engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(infra_db._ratelimit_engine.sync_engine, "before_cursor_execute", before)


async def test_the_preinserted_row_loads_an_empty_log_only_list(clients):
    assert await blocklist_sync.poll_once()
    bl = blocklist_state.current()
    assert (bl.version, bl.mode, bl.active(now=0)) == (0, "log_only", 0)


async def test_a_missing_version_row_is_a_failure_not_an_empty_list(clients, alerts):
    async with session_maker() as db:
        await db.execute(BlocklistVersion.__table__.delete())
        await db.commit()
    assert await blocklist_sync.poll_once() is False
    assert blocklist_state.current() is None
    assert "ratelimit_blocklist_load_failed" in [e for e, _ in alerts]


async def test_the_list_reloads_only_when_the_version_changes(clients):
    await _write(3, "41", "42")
    assert await blocklist_sync.poll_once()
    assert blocklist_state.current().version == 3 and blocklist_state.current().active(now=0) == 2
    seen, stop = _statements()
    try:
        assert await blocklist_sync.poll_once()  # unchanged: one primary-key read, no entry scan
    finally:
        stop()
    assert len(seen) == 1 and "blocklistversion" in seen[0] and "blocklistentry" not in seen[0]
    await _write(4, "43")
    assert await blocklist_sync.poll_once()
    assert blocklist_state.current().version == 4 and blocklist_state.current().active(now=0) == 3
    assert blocklist_state.counters()["blocklist_reloads"] == 2


async def test_revoked_and_expired_entries_are_not_loaded(clients):
    await _write(5, "51", revoked=True)
    async with session_maker() as db:
        now = utcnow_naive()
        db.add(BlocklistEntry(kind="org_id", value="52", reason="r", approved_by="o", evidence={"q": 1},
                              created_by="t", created_at=now - timedelta(days=2), expires_at=now - timedelta(days=1)))
        await db.commit()
    assert await blocklist_sync.poll_once()
    assert blocklist_state.current().active(now=0) == 0


async def test_a_failed_poll_keeps_the_cache_and_alerts_once(clients, monkeypatch, alerts, caplog):
    await _write(3, "41")
    assert await blocklist_sync.poll_once()
    good = blocklist_state.current()

    def broken():
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(blocklist_sync, "ratelimit_session_maker", broken)
    caplog.set_level(logging.ERROR, logger="treg.ratelimit")
    for _ in range(3):
        assert await blocklist_sync.poll_once() is False
    assert blocklist_state.current() is good  # never emptied, never replaced
    assert blocklist_state.counters()["blocklist_load_errors"] == 3
    assert [e for e, _ in alerts].count("ratelimit_blocklist_load_failed") == 1
    assert sum(r.getMessage().startswith("rate_limit_blocklist_load_failed") for r in caplog.records) == 1


async def test_a_hanging_connection_is_bounded_by_the_poll_timeout(clients, monkeypatch, alerts):
    """A connection that never answers (no TCP reset) cannot hold a poll, or the first load, open."""
    monkeypatch.setenv("TREG_RATE_LIMIT_BLOCKLIST_POLL_TIMEOUT_S", "0.2")
    get_settings.cache_clear()

    class Hang:
        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(blocklist_sync, "ratelimit_session_maker", Hang)
    started = time.monotonic()
    assert await blocklist_sync.poll_once() is False
    assert time.monotonic() - started < 1.0
    assert "ratelimit_blocklist_load_failed" in [e for e, _ in alerts]


async def test_startup_with_a_reachable_database_serves_with_the_list_loaded(clients, caplog):
    await _write(3, "41")
    caplog.set_level(logging.INFO, logger="treg.ratelimit")
    assert await blocklist_sync.first_load() is True
    assert blocklist_state.current().version == 3
    [line] = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
              if r.getMessage().startswith("rate_limit_blocklist_first_load ")]
    assert line["ok"] is True and line["pid"] == os.getpid() and line["elapsed_ms"] >= 0
    assert line["version"] == 3 and line["entries"] == 1


async def test_a_first_load_past_its_cap_falls_to_background_retry(clients, monkeypatch, alerts, caplog):
    monkeypatch.setenv("TREG_RATE_LIMIT_BLOCKLIST_FIRST_LOAD_S", "0.2")
    monkeypatch.setenv("TREG_RATE_LIMIT_BLOCKLIST_POLL_TIMEOUT_S", "10")  # the first-load cap decides
    get_settings.cache_clear()
    real = blocklist_sync.ratelimit_session_maker

    class Slow:
        async def __aenter__(self):
            await asyncio.Event().wait()  # never answers

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(blocklist_sync, "ratelimit_session_maker", Slow)
    caplog.set_level(logging.INFO, logger="treg.ratelimit")
    started = time.monotonic()
    assert await blocklist_sync.first_load() is False
    assert time.monotonic() - started < 1.0  # capped, the worker starts serving now
    [line] = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
              if r.getMessage().startswith("rate_limit_blocklist_first_load ")]
    assert line["ok"] is False and line["pid"] == os.getpid() and line["elapsed_ms"] < 1000
    assert ("blocklist_cache_empty", {"value": 1}) in alerts
    assert background.process_status()["blocklist_cache_empty"] is True
    # Fail open, and health never depends on the blocklist.
    assert (await clients.get("/tools")).status_code == 200
    assert (await clients.get("/health")).status_code == 200
    # The background loop retries quickly while empty, and clears the gauge once a load succeeds.
    def refused():
        raise ConnectionError("still down")

    monkeypatch.setattr(blocklist_sync, "ratelimit_session_maker", refused)
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 1:
            monkeypatch.setattr(blocklist_sync, "ratelimit_session_maker", real)
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(background.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await background.blocklist_loop()
    assert sleeps == [background._EMPTY_RETRY_S, get_settings().rate_limit_blocklist_refresh_s]
    assert blocklist_state.current() is not None
    assert background.process_status()["blocklist_cache_empty"] is False


async def test_startup_makes_one_capped_attempt_and_goes_on_when_it_fails(monkeypatch):
    from cryptography.fernet import Fernet

    from treg import bootstrap
    from treg.bootstrap import create_app
    from treg.infra.db import reset_db
    calls = []

    async def fake_first_load():
        calls.append("first_load")
        return False  # the database is down: the worker must still start

    # Lifespan verifies the schema before the first load. Postgres refuses to boot without a
    # secret key; sqlite does not. Stamp the head either way so the test does not depend on
    # whichever test ran before it.
    monkeypatch.setenv("TREG_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    await reset_db()
    monkeypatch.setattr(bootstrap.rate_limit_blocklist_sync, "first_load", fake_first_load)
    app = create_app("control")
    async with app.router.lifespan_context(app):
        assert calls == ["first_load"]  # one attempt, and startup went on although it failed


async def test_an_over_limit_reload_keeps_the_last_good_list_and_alerts(clients, monkeypatch, alerts):
    await _write(3, "41")
    assert await blocklist_sync.poll_once()
    good = blocklist_state.current()
    monkeypatch.setenv("TREG_RATE_LIMIT_BLOCKLIST_MAX_ENTRIES", "2")
    get_settings.cache_clear()
    await _write(4, "42", "43")  # three active entries now, past the limit of two
    assert await blocklist_sync.poll_once() is False
    assert blocklist_state.current() is good  # not truncated, not replaced
    [(event, props)] = [(e, p) for e, p in alerts if e == "ratelimit_blocklist_load_failed"]
    assert "TooManyEntries" in props["reason"] and props["kept_version"] == 3


async def test_version_mode_and_entries_come_from_one_snapshot(clients, monkeypatch):
    """A write landing between the version read and the reload cannot leave a new version cached
    with old entries: the reload reads all three in one statement."""
    await _write(3, "41")
    assert await blocklist_sync.poll_once()
    real = blocklist_sync._read_version

    async def read_then_a_write_lands(db):
        head = await real(db)
        await _write(5, "42")  # another writer commits between the two reads
        return (4, "log_only")  # an intermediate version this process never saw

    monkeypatch.setattr(blocklist_sync, "_read_version", read_then_a_write_lands)
    assert await blocklist_sync.poll_once()
    bl = blocklist_state.current()
    assert bl.version == 5 and bl.match("org_id", "42", now=0) is not None  # version and entries agree


async def test_cancelling_the_background_loops_during_a_poll_returns(monkeypatch):
    """Lifespan shutdown cancels the loops. The poll already inside a session must finish and close
    it; tearing that close down never returns, and the worker would not shut down."""
    await reset_db()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holding():
        async with blocklist_sync.ratelimit_session_maker() as db:
            await db.execute(select(BlocklistVersion.id).where(BlocklistVersion.id == 1))
            entered.set()
            await release.wait()

    monkeypatch.setattr(blocklist_sync, "_poll", holding)
    task = asyncio.create_task(background.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3)
            except (asyncio.CancelledError, TimeoutError):
                pass


def test_the_ratelimit_pool_has_a_statement_timeout(monkeypatch):
    captured = {}
    monkeypatch.setattr(infra_db, "create_async_engine", lambda url, **kw: captured.update(kw) or _Engine())
    monkeypatch.setattr(infra_db.money_trace, "install_engine", lambda *a, **k: None)
    infra_db._new_engine("ratelimit", "postgresql+asyncpg://u:p@db/treg")
    assert captured["connect_args"] == {"server_settings": {"statement_timeout": "2000"}, "timeout": 2.0}
    assert captured["pool_size"] == 1 and captured["max_overflow"] == 0


class _Engine:
    sync_engine = None
