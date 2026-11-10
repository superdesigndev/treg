"""Observation gates and bucket values share a snapshot on the isolated PostgreSQL test DB."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from treg.application import catalog_stats
from treg.domain.catalog import stats, store as catalog_store
from treg.infra.catalog_observations import (
    EndpointObservationUnavailable, PostgresEndpointObservationReader, STALE_AFTER_S,
)
from treg.infra.db import reset_db, session_maker
from treg.models import CallRecord, EndpointDayStat, EndpointStatCursor, EndpointStatDirty
from treg import timeutil

NOW = datetime(2026, 10, 15, 12)
SUBMITTED = NOW - timedelta(minutes=5)
DAY = NOW.strftime("%Y-%m-%d")
EP = "test.observation.async"


@pytest.fixture
def observation_catalog(monkeypatch):
    cat = catalog_store.load()
    monkeypatch.setitem(cat.by_id, EP, {"id": EP, "async": {"state": "status"}})
    monkeypatch.setitem(cat.adapters, EP, object())
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: False)
    monkeypatch.setattr(stats, "_now", lambda: NOW)
    monkeypatch.setattr(timeutil, "utcnow_naive", lambda: NOW)


async def _seed_overdue(*, with_bucket: bool) -> None:
    async with session_maker() as db:
        rows = [CallRecord(
            user_email="synthetic@example.test", tool_name=EP, method="POST", path="/synthetic",
            endpoint_id=EP, status_code=200, duration_ms=100, hit=True, verdict="valid",
            created_at=SUBMITTED,
        ) for _ in range(24)] if with_bucket else []
        db.add_all(rows)
        await db.flush()
        db.add(EndpointStatCursor(
            id="callrecord", cursor_id=rows[-1].id if rows else 0,
            updated_at=NOW, caught_up_at=NOW,
        ))
        if with_bucket:
            db.add(EndpointDayStat(
                endpoint_id=EP, day=DAY, n=24, ok=24, hits=0, hit_decided=24,
                latency_seen=24, latency_sample=[100] * 24, last_ok_at=SUBMITTED,
                updated_at=SUBMITTED, terminal_reconciled=True,
            ))
        queued = NOW - timedelta(seconds=STALE_AFTER_S + 1)
        db.add(EndpointStatDirty(endpoint_id=EP, day=DAY, queued_at=queued, retry_at=queued))
        await db.commit()


async def test_overdue_correction_without_a_bucket_is_not_published_as_zero_samples(
    observation_catalog,
):
    await reset_db()
    await _seed_overdue(with_bucket=False)
    with pytest.raises(EndpointObservationUnavailable) as caught:
        await PostgresEndpointObservationReader(session_maker).get_many([EP])
    assert caught.value.completed == {}


async def test_repair_committing_after_bucket_read_cannot_renew_its_old_value(
    observation_catalog,
):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await reset_db()
    await _seed_overdue(with_bucket=True)
    bucket_read, resume = asyncio.Event(), asyncio.Event()

    class PausingReaderSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if (not bucket_read.is_set() and any(
                item.get("entity") is EndpointDayStat
                for item in getattr(statement, "column_descriptions", ())
            )):
                bucket_read.set()
                await resume.wait()
            return result

    reader_sessions = async_sessionmaker(engine, class_=PausingReaderSession, expire_on_commit=False)
    reader = PostgresEndpointObservationReader(reader_sessions)
    read = asyncio.create_task(reader.get_many([EP]))
    try:
        async with asyncio.timeout(15):
            await bucket_read.wait()
            # A real, separate transaction repairs the source rows and clears the overdue marker
            # before the reader asks for dirty state. It must not be blocked by this reader.
            assert await catalog_stats._repair_bucket(session_maker, EP, DAY, NOW)
            async with session_maker() as db:
                bucket = await db.get(EndpointDayStat, (EP, DAY))
                assert bucket.hits == bucket.hit_decided == 24 and bucket.terminal_reconciled
                assert await db.get(EndpointStatDirty, (EP, DAY)) is None
            resume.set()
            with pytest.raises(EndpointObservationUnavailable) as caught:
                await read
            assert caught.value.completed == {}, "old values must retain their old overdue gate"

            # A new snapshot sees the repair, and can publish the corrected measurement.
            fresh = await reader.get_many([EP])
            assert fresh[EP]["samples"] == fresh[EP]["hit_samples"] == 24
            assert fresh[EP]["hit_rate"] == 1.0
    finally:
        resume.set()
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)
