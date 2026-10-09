"""Optional catalog observations must not turn missing folds into audit-table scans."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from treg.infra.catalog_observations import PostgresEndpointObservationReader
from treg.infra import catalog_observations
from treg.domain.catalog import stats, store
from treg.infra.db import session_maker
from treg.models import EndpointDayStat, EndpointStatCursor
from treg.timeutil import utcnow_naive


@pytest.mark.parametrize("state", ["missing", "backfill", "stale", "caught_up"])
async def test_sync_catalog_refresh_never_scans_callrecord(clients, state):
    now = utcnow_naive()
    async with session_maker() as db:
        if state != "missing":
            at = now - timedelta(hours=3) if state == "stale" else now
            db.add(EndpointStatCursor(id="callrecord", updated_at=at,
                                     caught_up_at=None if state == "backfill" else at))
            await db.commit()
        engine = db.get_bind()

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM callrecord" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        await PostgresEndpointObservationReader(session_maker).get_many(
            [f"sync.endpoint.{i}" for i in range(200)])
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements == [], "optional refresh executed a live callrecord aggregate"


async def test_running_worker_that_never_catches_up_is_stale(clients, caplog):
    now = utcnow_naive()
    async with session_maker() as db:
        db.add(EndpointStatCursor(id="callrecord", updated_at=now,
                                 caught_up_at=now - timedelta(hours=3)))
        await db.commit()
    reader = PostgresEndpointObservationReader(session_maker)
    assert await reader.get_many(["sync.endpoint"]) == {}
    assert "catalog_stats_unavailable reason=stale" in caplog.text


async def test_async_siblings_are_batched_without_losing_terminal_evidence(clients, monkeypatch):
    ids = [f"async.endpoint.{i}" for i in range(25)]
    monkeypatch.setattr(store, "load", lambda: SimpleNamespace(by_id={i: {"async": True} for i in ids}))
    batches = []

    async def observed(db, endpoint_ids, *, per_success):
        assert per_success == set()  # only terminal verdicts decide async hit rate
        assert len(endpoint_ids) <= catalog_observations.LIVE_BATCH_ENDPOINTS
        batches.append(endpoint_ids)
        return {i: {"samples": 20, "hit_samples": 20, "hit_rate": 0.6} for i in endpoint_ids}

    monkeypatch.setattr(stats, "observed", observed)
    result = await PostgresEndpointObservationReader(session_maker).get_many(ids)
    assert [i for batch in batches for i in batch] == ids
    assert set(result) == set(ids)
    assert all(row["hit_rate"] == 0.6 for row in result.values())


async def test_read_deadline_cancels_live_work_and_closes_session(clients, monkeypatch):
    closed = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    monkeypatch.setattr(catalog_observations, "READ_TIMEOUT_S", 0.05)

    @asynccontextmanager
    async def sessions():
        try:
            async with session_maker() as db:
                yield db
        finally:
            closed.set()

    async def observed(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(stats, "observed", observed)
    with pytest.raises(TimeoutError):
        await PostgresEndpointObservationReader(sessions).get_many(["wiza.people.email.find"])
    assert started.is_set() and cancelled.is_set() and closed.is_set()


async def test_postgres_statement_deadline_is_local_and_uses_timeout_backoff(clients, monkeypatch):
    async with session_maker() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("Postgres statement cancellation requires the isolated Postgres test DB")
        before = (await db.execute(text("SHOW statement_timeout"))).scalar_one()
        now = utcnow_naive()
        db.add(EndpointStatCursor(id="callrecord", updated_at=now, caught_up_at=now))
        db.add(EndpointDayStat(endpoint_id="sync.endpoint", day=now.date().isoformat(), n=6, ok=6))
        await db.commit()
    monkeypatch.setattr(catalog_observations, "STATEMENT_TIMEOUT_MS", 25)

    async def observed(db, *args, **kwargs):
        await db.execute(text("SELECT pg_sleep(1)"))
        pytest.fail("the statement deadline did not interrupt the live read")

    monkeypatch.setattr(stats, "observed", observed)
    with pytest.raises(catalog_observations.EndpointObservationTimeout) as raised:
        await PostgresEndpointObservationReader(session_maker).get_many(
            ["sync.endpoint", "wiza.people.email.find"])
    assert raised.value.completed["sync.endpoint"]["samples"] == 6
    assert "wiza.people.email.find" not in raised.value.completed
    async with session_maker() as db:
        assert (await db.execute(text("SHOW statement_timeout"))).scalar_one() == before
