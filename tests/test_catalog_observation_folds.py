"""Request-triggered observation reads stay on trustworthy day buckets, never raw audit rows."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event

from treg.domain.catalog import stats, store
from treg.infra.catalog_observations import (
    EndpointObservationUnavailable, PostgresEndpointObservationReader, STALE_AFTER_S,
)
from treg.infra.db import session_maker
from treg.models import EndpointDayStat, EndpointStatCursor, EndpointStatDirty
from treg.timeutil import utcnow_naive

SYNC = "test.fold.sync"
ASYNC = "test.fold.async"
NO_ADAPTER = "test.fold.async_without_adapter"


@pytest.fixture
def catalog_endpoints(monkeypatch):
    cat = store.load()
    for endpoint_id, asynchronous in ((SYNC, False), (ASYNC, True), (NO_ADAPTER, True)):
        monkeypatch.setitem(cat.by_id, endpoint_id, {
            "id": endpoint_id, "async": {"state": "status"} if asynchronous else None,
            "cost": {"type": "per_success"},
        })
    monkeypatch.setitem(cat.adapters, ASYNC, object())
    return cat


async def _cursor(**overrides):
    values = {"id": "callrecord", "updated_at": utcnow_naive(), "caught_up_at": utcnow_naive()}
    values.update(overrides)
    async with session_maker() as db:
        db.add(EndpointStatCursor(**values))
        await db.commit()


async def _bucket(endpoint_id, *, reconciled=True, day=None, **overrides):
    values = dict(endpoint_id=endpoint_id, day=day or utcnow_naive().strftime("%Y-%m-%d"),
                  n=24, ok=24, hits=12, hit_decided=20, paid_hits=2, free_misses=2,
                  last_ok_at=utcnow_naive(), latency_seen=24, latency_sample=[100] * 24,
                  terminal_reconciled=reconciled)
    values.update(overrides)
    async with session_maker() as db:
        db.add(EndpointDayStat(**values))
        await db.commit()


@pytest.fixture
def no_raw_observation_reads(monkeypatch):
    async def no_live(*args, **kwargs):
        raise AssertionError("a request-triggered refresh must never call stats.observed")

    monkeypatch.setattr(stats, "observed", no_live)
    engine = session_maker.kw["bind"].sync_engine
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record)
    yield statements
    event.remove(engine, "before_cursor_execute", record)
    assert not any("callrecord." in sql or "from callrecord " in sql for sql in statements)


@pytest.mark.parametrize("state", ["missing", "backlog", "worker_stale", "caught_up_stale"])
async def test_unavailable_cursor_never_falls_back_to_audit(
    clients, catalog_endpoints, no_raw_observation_reads, state,
):
    old = utcnow_naive() - timedelta(seconds=STALE_AFTER_S + 1)
    if state != "missing":
        await _cursor(**({"caught_up_at": None} if state == "backlog" else
                        {"updated_at": old} if state == "worker_stale" else {"caught_up_at": old}))
    await _bucket(SYNC)
    with pytest.raises(EndpointObservationUnavailable) as caught:
        await PostgresEndpointObservationReader(session_maker).get_many([SYNC, ASYNC])
    assert caught.value.completed == {}, "incomplete or old folds must not be renewed as measurements"


async def test_async_observations_read_reconciled_buckets_without_charge_inference(
    clients, catalog_endpoints, no_raw_observation_reads,
):
    await _cursor()
    for endpoint_id in (SYNC, ASYNC, NO_ADAPTER):
        await _bucket(endpoint_id)
    got = await PostgresEndpointObservationReader(session_maker).get_many([SYNC, ASYNC, NO_ADAPTER])
    assert got[ASYNC]["hit_samples"] == got[NO_ADAPTER]["hit_samples"] == 20
    assert got[ASYNC]["hit_rate"] == got[NO_ADAPTER]["hit_rate"] == 0.6
    assert got[SYNC]["hit_samples"] == 24 and got[SYNC]["hit_rate"] == round(14 / 24, 4)
    assert got[ASYNC]["p50_ms"] == got[ASYNC]["p95_ms"] == 100


async def test_one_unreconciled_async_day_preserves_other_endpoints_as_partial_results(
    clients, catalog_endpoints, no_raw_observation_reads,
):
    await _cursor()
    await _bucket(SYNC, reconciled=False)
    await _bucket(NO_ADAPTER, reconciled=False)
    await _bucket(ASYNC)
    await _bucket(ASYNC, reconciled=False,
                  day=(utcnow_naive() - timedelta(days=1)).strftime("%Y-%m-%d"))
    with pytest.raises(EndpointObservationUnavailable) as caught:
        await PostgresEndpointObservationReader(session_maker).get_many([SYNC, ASYNC, NO_ADAPTER, "uncalled"])
    got = caught.value.completed
    assert set(got) == {SYNC, NO_ADAPTER, "uncalled"}
    assert got[SYNC]["samples"] == got[NO_ADAPTER]["samples"] == 24
    assert got["uncalled"]["samples"] == 0, "genuine zero samples remain a complete, cacheable result"


@pytest.mark.parametrize("age,blocked", [(60, False), (STALE_AFTER_S + 1, True)])
async def test_async_dirty_age_uses_first_queue_time_even_when_retry_is_in_the_future(
    clients, catalog_endpoints, no_raw_observation_reads, age, blocked,
):
    await _cursor()
    await _bucket(SYNC)
    await _bucket(ASYNC)
    async with session_maker() as db:
        for endpoint_id in (SYNC, ASYNC):
            db.add(EndpointStatDirty(
                endpoint_id=endpoint_id, day=utcnow_naive().strftime("%Y-%m-%d"),
                queued_at=utcnow_naive() - timedelta(seconds=age),
                retry_at=utcnow_naive() + timedelta(days=1)))
        await db.commit()
    reader = PostgresEndpointObservationReader(session_maker)
    if blocked:
        with pytest.raises(EndpointObservationUnavailable) as caught:
            await reader.get_many([SYNC, ASYNC])
        assert set(caught.value.completed) == {SYNC}
    else:
        assert set(await reader.get_many([SYNC, ASYNC])) == {SYNC, ASYNC}


async def test_old_or_pre_replacement_async_buckets_and_dirty_markers_do_not_block_current_evidence(
    clients, catalog_endpoints, no_raw_observation_reads, monkeypatch,
):
    await _cursor()
    await _bucket(ASYNC)
    at = utcnow_naive()
    monkeypatch.setitem(catalog_endpoints.by_id[ASYNC], "observed_from", at.strftime("%Y-%m-%d"))
    for age in (1, 40):
        day = (at - timedelta(days=age)).strftime("%Y-%m-%d")
        await _bucket(ASYNC, day=day, reconciled=False)
        async with session_maker() as db:
            db.add(EndpointStatDirty(endpoint_id=ASYNC, day=day,
                                     queued_at=at - timedelta(days=1), retry_at=at))
            await db.commit()
    got = await PostgresEndpointObservationReader(session_maker).get_many([ASYNC])
    assert got[ASYNC]["samples"] == 24 and got[ASYNC]["hit_rate"] == 0.6
