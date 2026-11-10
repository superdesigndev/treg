"""Late terminal evidence is folded into the original UTC day, once and off the read path.

These exercise the public worker against the isolated test database. The SQL event checks also
pin the important boundary: a recount reads one endpoint/day only through the persisted fold
cursor, never locks source audit rows, and does not consume a future incremental batch.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncResult

from treg.application import catalog_stats
from treg.domain.catalog import stats
from treg.domain.catalog import store as catalog_store
from treg.infra.catalog_stat_dirty import mark_dirty
from treg.infra.db import session_maker
from treg.models import CallRecord, EndpointDayStat, EndpointStatCursor, EndpointStatDirty

NOW = datetime(2026, 10, 15, 12)
YESTERDAY = datetime(2026, 10, 14, 23)
ASYNC = "wiza.people.email.find"
SYNC = "tikhub.tiktok.user.profile"


def _day(at: datetime) -> str:
    return at.strftime("%Y-%m-%d")


async def _record(endpoint: str, *, at: datetime = YESTERDAY, hit: bool | None = None,
                  ms: int | None = 100, status: int = 200, cost: int | None = 0,
                  refused_by: str | None = None, verdict: str | None = None) -> int:
    async with session_maker() as db:
        row = CallRecord(org_id=1, user_email="a@b.c", tool_name=endpoint, method="GET",
                         path="/x", endpoint_id=endpoint, status_code=status, duration_ms=ms,
                         hit=hit, cost_observed_micro=cost, refused_by=refused_by,
                         verdict=verdict, created_at=at)
        db.add(row)
        await db.commit()
        return row.id


async def _bucket(endpoint: str = ASYNC, at: datetime = YESTERDAY) -> EndpointDayStat | None:
    async with session_maker() as db:
        return await db.get(EndpointDayStat, (endpoint, _day(at)))


async def _dirty() -> list[EndpointStatDirty]:
    async with session_maker() as db:
        return list((await db.execute(select(EndpointStatDirty).order_by(
            EndpointStatDirty.endpoint_id, EndpointStatDirty.day))).scalars())


async def _correct(ids: list[int], *, hit: bool, verdict: str | None = None,
                   now: datetime = NOW) -> None:
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.id.in_(ids)))).scalars()
        keys = set()
        for row in rows:
            row.hit, row.verdict = hit, verdict
            keys.add((row.endpoint_id, _day(row.created_at)))
            db.add(row)
        await mark_dirty(db, keys, now=now)
        await db.commit()


def _is_recount(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return "from callrecord" in normalized and "callrecord.id <=" in normalized


@contextmanager
def _capture_recounts(callback=None):
    engine = session_maker.kw["bind"].sync_engine
    seen = []

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if _is_recount(statement):
            seen.append((statement, parameters))
            if callback is not None:
                callback(connection, statement, parameters)

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)


async def test_terminal_hits_recount_the_original_day_and_complete_evidence(clients, monkeypatch):
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: False)
    ids = [await _record(ASYNC) for _ in range(20)]
    assert (await catalog_stats.refresh(session_maker, now=NOW))["caught_up"]
    before = await _bucket()
    assert before.terminal_reconciled and before.hit_decided == 0

    await _correct(ids[:12], hit=True, verdict="valid")
    await _correct(ids[12:], hit=False, verdict="not_found")
    result = await catalog_stats.refresh(session_maker, now=NOW)

    assert result["rows"] == 0 and result["repaired"] == 1
    assert result["repair_failed"] == result["repair_pending"] == 0
    bucket = await _bucket()
    assert (bucket.n, bucket.ok, bucket.bad) == (20, 20, 0)
    assert (bucket.hits, bucket.hit_decided) == (12, 20)
    assert bucket.paid_hits == bucket.free_misses == 0
    assert bucket.verdicts == {"valid": 12, "not_found": 8}
    assert bucket.last_ok_at == YESTERDAY and bucket.latency_seen == 20
    assert bucket.latency_sample == [100] * 20
    assert bucket.terminal_reconciled and await _dirty() == []
    assert await _bucket(at=NOW) is None  # completion day must not receive the submission's tally


async def test_terminal_misses_replace_success_last_ok_and_latency(clients, monkeypatch):
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda ep: ep == ASYNC)
    ids = [await _record(ASYNC, at=YESTERDAY + timedelta(minutes=n), ms=100 * (n + 1))
           for n in range(3)]
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(ids[1:], hit=False, verdict="not_found")
    assert (await catalog_stats.refresh(session_maker, now=NOW))["repaired"] == 1
    bucket = await _bucket()
    assert (bucket.n, bucket.ok, bucket.bad) == (3, 1, 2)
    assert bucket.last_ok_at == YESTERDAY
    assert bucket.latency_seen == 1 and bucket.latency_sample == [100]
    assert bucket.hit_decided == 2 and bucket.hits == 0
    assert bucket.verdicts == {"not_found": 2}

    await _correct(ids[:1], hit=False, verdict="not_found")
    await catalog_stats.refresh(session_maker, now=NOW)
    bucket = await _bucket()
    assert (bucket.n, bucket.ok, bucket.bad) == (3, 0, 3)
    assert bucket.last_ok_at is None
    assert bucket.latency_seen == 0 and bucket.latency_sample == []
    assert bucket.verdicts == {"not_found": 3}


async def test_repeated_completion_markers_coalesce_and_recounts_are_idempotent(clients):
    ids = [await _record(ASYNC) for _ in range(4)]
    await catalog_stats.refresh(session_maker, now=NOW)
    for row_id in ids:
        await _correct([row_id], hit=True)
    marker, = await _dirty()
    assert marker.revision == 4
    with _capture_recounts() as queries:
        result = await catalog_stats.refresh(session_maker, now=NOW)
    assert result["repaired"] == 1 and len(queries) == 1
    first = await _bucket()
    assert first.n == first.ok == first.hits == first.hit_decided == 4

    await _correct(ids, hit=True)
    again = await catalog_stats.refresh(session_maker, now=NOW)
    second = await _bucket()
    assert again["repaired"] == 1
    for field in ("n", "ok", "bad", "hits", "hit_decided", "paid_hits", "free_misses",
                  "last_ok_at", "latency_seen", "latency_sample", "verdicts"):
        assert getattr(second, field) == getattr(first, field)


async def test_recount_stops_at_fold_cursor_then_new_rows_fold_exactly_once(clients):
    first = await _record(ASYNC)
    await catalog_stats.refresh(session_maker, now=NOW)
    second = await _record(ASYNC, at=YESTERDAY + timedelta(minutes=1), hit=True)
    await _correct([first], hit=True)

    with _capture_recounts() as queries:
        repaired = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
    assert repaired["rows"] == 0 and repaired["repaired"] == 1
    assert (await _bucket()).n == 1
    assert len(queries) == 1
    sql = " ".join(queries[0][0].lower().split())
    assert "callrecord.endpoint_id =" in sql
    assert "callrecord.created_at >=" in sql and "callrecord.created_at <" in sql
    assert "for update" not in sql
    async with session_maker() as db:
        cursor = await db.get(EndpointStatCursor, catalog_stats.CURSOR_ID)
        assert cursor.cursor_id == first < second

    assert (await catalog_stats.refresh(session_maker, now=NOW))["rows"] == 1
    bucket = await _bucket()
    assert bucket.n == bucket.hits == bucket.hit_decided == 2


async def test_old_async_buckets_backfill_once_but_immutable_buckets_do_not(clients, monkeypatch):
    no_adapter = "test.async.no_terminal_adapter"
    catalog = catalog_store.load()
    monkeypatch.setitem(catalog.by_id, no_adapter, {
        "id": no_adapter, "async": {"state": "status"}, "cost": {"type": "per_success"},
    })
    for endpoint in (ASYNC, SYNC, no_adapter):
        await _record(endpoint, hit=True)
    await catalog_stats.refresh(session_maker, now=NOW)
    async with session_maker() as db:
        for endpoint in (ASYNC, SYNC, no_adapter):
            bucket = await db.get(EndpointDayStat, (endpoint, _day(YESTERDAY)))
            bucket.terminal_reconciled = False
            bucket.hits = bucket.hit_decided = 0
            db.add(bucket)
        await db.commit()

    first = await catalog_stats.refresh(session_maker, max_rows=0, max_dirty_buckets=0, now=NOW)
    marker, = await _dirty()
    assert marker.endpoint_id == ASYNC and marker.revision == 1
    assert first["repair_pending"] == 1
    await catalog_stats.refresh(session_maker, max_rows=0, max_dirty_buckets=0, now=NOW)
    assert (await _dirty())[0].revision == 1  # re-seeding is not a new terminal correction

    repaired = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
    assert repaired["repaired"] == 1 and await _dirty() == []
    assert (await _bucket()).terminal_reconciled and (await _bucket()).hits == 1
    assert not (await _bucket(SYNC)).terminal_reconciled
    assert not (await _bucket(no_adapter)).terminal_reconciled
    assert (await catalog_stats.refresh(session_maker, max_rows=0, now=NOW))["repaired"] == 0


async def test_incremental_fold_does_not_claim_to_reconcile_a_legacy_bucket(clients):
    await _record(ASYNC)
    await catalog_stats.refresh(session_maker, now=NOW)
    async with session_maker() as db:
        bucket = await db.get(EndpointDayStat, (ASYNC, _day(YESTERDAY)))
        bucket.terminal_reconciled = False
        db.add(bucket)
        await db.commit()
    await _record(ASYNC, at=YESTERDAY + timedelta(minutes=1))
    result = await catalog_stats.refresh(session_maker, max_dirty_buckets=0, now=NOW)
    assert result["rows"] == 1 and result["repair_pending"] == 1
    bucket = await _bucket()
    assert bucket.n == 2 and not bucket.terminal_reconciled


async def test_dirty_bucket_budget_leaves_work_for_the_next_run(clients):
    endpoints = ["test.repair.a", "test.repair.b", "test.repair.c"]
    ids = [await _record(endpoint) for endpoint in endpoints]
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(ids, hit=True)
    with _capture_recounts() as queries:
        first = await catalog_stats.refresh(session_maker, max_dirty_buckets=1, now=NOW)
    assert first["repaired"] == 1 and first["repair_pending"] == 2 and len(queries) == 1
    with _capture_recounts() as queries:
        second = await catalog_stats.refresh(session_maker, max_dirty_buckets=1, now=NOW)
    assert second["repaired"] == 1 and second["repair_pending"] == 1 and len(queries) == 1
    third = await catalog_stats.refresh(session_maker, max_dirty_buckets=1, now=NOW)
    assert third["repaired"] == 1 and third["repair_pending"] == 0
    for endpoint in endpoints:
        bucket = await _bucket(endpoint)
        assert bucket.n == bucket.hits == bucket.hit_decided == 1


async def test_timed_out_bucket_keeps_old_tally_and_does_not_starve_the_next(clients):
    hot, cool = "test.repair.a_hot", "test.repair.b_cool"
    hot_id, cool_id = [await _record(endpoint) for endpoint in (hot, cool)]
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct([hot_id, cool_id], hit=True)
    before = await _bucket(hot)

    def fail_hot(connection, statement, parameters):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if hot in values:
            raise TimeoutError("synthetic local bucket timeout")

    with _capture_recounts(fail_hot) as queries:
        result = await catalog_stats.refresh(session_maker, max_dirty_buckets=5, now=NOW)
    assert result["repaired"] == result["repair_failed"] == result["repair_pending"] == 1
    assert len(queries) == 2  # the failed marker is attempted only once this run
    after = await _bucket(hot)
    assert after.n == before.n == 1 and after.hits == before.hits == 0
    assert after.updated_at == before.updated_at
    assert (await _bucket(cool)).hits == 1
    marker, = await _dirty()
    assert marker.endpoint_id == hot and marker.retry_at > NOW
    assert marker.queued_at == NOW and marker.revision == 1

    with _capture_recounts() as queries:
        deferred = await catalog_stats.refresh(session_maker, now=NOW)
    assert deferred["repaired"] == 0 and deferred["repair_pending"] == 1 and not queries
    later = await catalog_stats.refresh(session_maker, now=marker.retry_at + timedelta(seconds=1))
    assert later["repaired"] == 1 and later["repair_pending"] == 0
    assert (await _bucket(hot)).hits == 1


async def test_zero_repair_time_budget_preserves_dirty_work(clients):
    row_id = await _record(ASYNC)
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct([row_id], hit=True)
    with _capture_recounts() as queries:
        result = await catalog_stats.refresh(session_maker, max_repair_seconds=0, now=NOW)
    assert result["repaired"] == 0 and result["repair_pending"] == 1 and not queries
    assert (await _bucket()).hits == 0
    assert (await catalog_stats.refresh(session_maker, now=NOW))["repaired"] == 1


async def test_total_repair_time_budget_stops_before_the_next_bucket(clients, monkeypatch):
    ids = [await _record(endpoint) for endpoint in ("test.repair.a", "test.repair.b")]
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(ids, hit=True)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(catalog_stats, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def spend_budget(connection, statement, parameters):
        clock.now = 2.0

    with _capture_recounts(spend_budget) as queries:
        first = await catalog_stats.refresh(session_maker, max_repair_seconds=1, now=NOW)
    assert first["repaired"] == first["repair_pending"] == 1 and len(queries) == 1
    clock.now = 0.0
    second = await catalog_stats.refresh(session_maker, max_repair_seconds=1, now=NOW)
    assert second["repaired"] == 1 and second["repair_pending"] == 0


async def test_new_revision_during_recount_is_not_acknowledged(clients):
    row_id = await _record(ASYNC)
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct([row_id], hit=True)

    def newer_revision(connection, statement, parameters):
        # Model a correction that commits after the worker reads revision 1. The source query
        # sees the current data, but acknowledging revision 1 still must not clear revision 2.
        connection.execute(update(EndpointStatDirty).where(
            EndpointStatDirty.endpoint_id == ASYNC).values(revision=EndpointStatDirty.revision + 1))

    with _capture_recounts(newer_revision) as queries:
        first = await catalog_stats.refresh(session_maker, now=NOW)
    assert first["repaired"] == first["repair_pending"] == 1 and len(queries) == 1
    marker, = await _dirty()
    assert marker.revision == 2
    assert (await _bucket()).hits == 1
    second = await catalog_stats.refresh(session_maker, now=NOW)
    assert second["repaired"] == 1 and second["repair_pending"] == 0
    assert (await _bucket()).n == (await _bucket()).hits == 1


async def test_failure_after_replacement_rolls_back_bucket_and_acknowledgement(clients):
    row_id = await _record(ASYNC)
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct([row_id], hit=True)
    engine = session_maker.kw["bind"].sync_engine
    fail = True

    def before_ack(connection, cursor, statement, parameters, context, executemany):
        nonlocal fail
        normalized = " ".join(statement.lower().split())
        if fail and normalized.startswith("delete from endpointstatdirty") and "revision =" in normalized:
            fail = False
            raise TimeoutError("synthetic local failure after bucket replacement")

    event.listen(engine, "before_cursor_execute", before_ack)
    try:
        result = await catalog_stats.refresh(session_maker, now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", before_ack)
    assert not fail and result["repair_failed"] == result["repair_pending"] == 1
    assert result["repaired"] == 0 and (await _bucket()).hits == 0
    marker, = await _dirty()
    assert marker.revision == 1 and marker.retry_at > NOW
    later = await catalog_stats.refresh(session_maker, now=marker.retry_at + timedelta(seconds=1))
    assert later["repaired"] == 1 and (await _bucket()).hits == 1


async def test_recount_keeps_the_complete_first_day_of_the_folded_window(clients):
    first_day = datetime.fromisoformat(stats.window_days(NOW))
    # The day bucket is deliberately wider than the live query's exact 30-day instant. These
    # rows were folded before the window advanced; repairing cannot silently shrink that day.
    early = first_day + timedelta(hours=1)
    late = first_day + timedelta(hours=20)
    ids = [await _record(ASYNC, at=at) for at in (early, late)]
    await catalog_stats.refresh(session_maker, now=NOW - timedelta(days=1))
    assert (await _bucket(at=early)).n == 2
    await _correct(ids, hit=True)
    result = await catalog_stats.refresh(session_maker, now=NOW)
    assert result["repaired"] == 1
    bucket = await _bucket(at=early)
    assert bucket.n == bucket.hits == bucket.hit_decided == 2


async def test_expired_dirty_buckets_are_pruned_without_scanning_audit_rows(clients):
    expired = NOW - timedelta(days=stats.WINDOW_DAYS + 2)
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=ASYNC, day=_day(expired), n=7, ok=7))
        await mark_dirty(db, {(ASYNC, _day(expired))}, now=NOW)
        await db.commit()
    with _capture_recounts() as queries:
        result = await catalog_stats.refresh(session_maker, now=NOW)
    assert not queries and result["repaired"] == result["repair_pending"] == 0
    assert await _dirty() == [] and await _bucket(at=expired) is None


async def test_recount_preserves_refusal_and_status_evidence_rules(clients, monkeypatch):
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: False)
    ids = [await _record(ASYNC, status=status, hit=None, verdict="unknown")
           for status in (200, 500, 405, 422)]
    await _record(ASYNC, status=402, refused_by="balance", hit=True, verdict="valid")
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(ids, hit=True, verdict="valid")
    result = await catalog_stats.refresh(session_maker, now=NOW)
    assert result["repaired"] == 1
    bucket = await _bucket()
    assert (bucket.n, bucket.ok, bucket.bad) == (4, 1, 2)
    assert bucket.hits == bucket.hit_decided == 4
    assert bucket.verdicts == {"valid": 4}
    assert bucket.latency_seen == 1 and bucket.latency_sample == [100]


@pytest.mark.parametrize("empty_fails", [False, True])
async def test_recount_keeps_complete_evidence_across_batches_and_a_partial_tail(
    clients, monkeypatch, empty_fails,
):
    """A busy day's replacement accumulates every batch, including the incomplete final one.

    Distinct status/hit/cost/verdict values catch swapped tuple fields as well as a tally reset or
    dropped tail. Keep the sample below its reservoir cap so the successful durations are exact.
    """
    monkeypatch.setattr(catalog_stats, "BATCH_ROWS", 4)
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: empty_fails)
    evidence = [
        # status, duration, hit, observed cost, refused_by, verdict
        (200, 100, True, 5000, None, "valid"),
        (202, 200, None, 5000, None, None),
        (200, 300, False, 0, None, "not_found"),
        (500, 400, False, None, None, "unknown"),
        (405, 500, None, None, None, None),
        (422, None, True, None, None, "valid"),
        (200, None, None, 0, None, None),
        (200, 700, True, 0, None, "catch_all"),
        (200, 800, False, 5000, None, "not_found"),
        (200, 900, True, 5000, "balance", "valid"),
        (503, None, None, None, None, "unknown"),
        (200, 1100, None, None, None, None),
        (200, 1200, True, 5000, None, "valid"),
        (200, 1300, False, 0, None, "not_found"),
    ]
    for n, (status, ms, hit, cost, refused_by, verdict) in enumerate(evidence):
        await _record(ASYNC, at=YESTERDAY + timedelta(minutes=n), status=status, ms=ms,
                      hit=hit, cost=cost, refused_by=refused_by, verdict=verdict)
    await catalog_stats.refresh(session_maker, batch_rows=4, now=NOW)
    async with session_maker() as db:
        bucket = await db.get(EndpointDayStat, (ASYNC, _day(YESTERDAY)))
        # Recount must replace stale evidence, rather than append to any of these values.
        bucket.n = bucket.ok = bucket.bad = bucket.hits = bucket.hit_decided = 99
        bucket.paid_hits = bucket.free_misses = bucket.latency_seen = 99
        bucket.last_ok_at = NOW
        bucket.latency_sample = [9999]
        bucket.verdicts = {"stale": 99}
        db.add(bucket)
        await mark_dirty(db, {(ASYNC, _day(YESTERDAY))}, now=NOW)
        await db.commit()

    result = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
    assert result["repaired"] == 1 and result["repair_failed"] == result["repair_pending"] == 0
    bucket = await _bucket()
    assert (bucket.n, bucket.ok, bucket.bad) == (13, 6 if empty_fails else 9,
                                               6 if empty_fails else 3)
    assert (bucket.hits, bucket.hit_decided, bucket.paid_hits, bucket.free_misses) == (4, 8, 1, 1)
    assert bucket.last_ok_at == YESTERDAY + timedelta(minutes=12 if empty_fails else 13)
    durations = [100, 200, 700, 1100, 1200] if empty_fails else [100, 200, 300, 700, 800, 1100, 1200, 1300]
    assert bucket.latency_seen == len(durations)
    assert sorted(bucket.latency_sample) == durations
    assert bucket.verdicts == {"valid": 3, "not_found": 3, "unknown": 2, "catch_all": 1}
    assert bucket.terminal_reconciled and await _dirty() == []


async def test_cancelled_recount_keeps_previous_bucket_and_dirty_work(clients, monkeypatch):
    monkeypatch.setattr(catalog_stats, "BATCH_ROWS", 2)
    ids = [await _record(ASYNC) for _ in range(5)]
    await catalog_stats.refresh(session_maker, now=NOW)
    await _correct(ids, hit=True, verdict="valid")
    async with session_maker() as db:
        bucket = await db.get(EndpointDayStat, (ASYNC, _day(YESTERDAY)))
        bucket.updated_at = NOW - timedelta(minutes=5)
        db.add(bucket)
        await db.commit()
        before = bucket.model_dump()

    original_fold = stats.Tally.fold
    folded = 0
    cancellation_requested = False
    repair = None

    def cancel_after_batch(tally, **fields):
        nonlocal folded, cancellation_requested
        result = original_fold(tally, **fields)
        if asyncio.current_task() is repair:
            folded += 1
            if folded >= catalog_stats.BATCH_ROWS and not cancellation_requested:
                cancellation_requested = True
                repair.cancel()
        return result

    monkeypatch.setattr(stats.Tally, "fold", cancel_after_batch)
    repair = asyncio.create_task(catalog_stats.refresh(session_maker, max_rows=0, now=NOW))
    with pytest.raises(asyncio.CancelledError):
        await repair
    assert cancellation_requested
    assert (await _bucket()).model_dump() == before
    marker, = await _dirty()
    assert marker.revision == 1

    monkeypatch.setattr(stats.Tally, "fold", original_fold)
    result = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
    assert result["repaired"] == 1 and result["repair_failed"] == result["repair_pending"] == 0
    bucket = await _bucket()
    assert bucket.n == bucket.hits == bucket.hit_decided == 5
    assert bucket.free_misses == 0 and bucket.verdicts == {"valid": 5}
    assert bucket.updated_at == NOW and await _dirty() == []


async def test_recount_stream_transports_only_upstream_evidence(clients, monkeypatch):
    monkeypatch.setattr(catalog_stats, "BATCH_ROWS", 4)
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: True)
    evidence = [
        (200, 100, True, 0, "valid"),
        (200, 200, False, 0, "not_found"),
        (500, 300, False, None, "unknown"),
        (422, None, None, None, None),
        (202, 400, None, 5000, None),
        (200, 500, True, 0, "catch_all"),
    ]
    minute = 0
    for status, ms, hit, cost, verdict in evidence:
        for refusal in ("balance", ""):
            await _record(ASYNC, at=YESTERDAY + timedelta(minutes=minute), status=200,
                          ms=9000, hit=True, cost=9999, refused_by=refusal, verdict="refused")
            minute += 1
        await _record(ASYNC, at=YESTERDAY + timedelta(minutes=minute), status=status,
                      ms=ms, hit=hit, cost=cost, verdict=verdict)
        minute += 1
    folded = await catalog_stats.refresh(session_maker, batch_rows=4, now=NOW)
    assert folded["rows"] == 3 * len(evidence) and folded["caught_up"]
    async with session_maker() as db:
        bucket = await db.get(EndpointDayStat, (ASYNC, _day(YESTERDAY)))
        bucket.n = 99
        db.add(bucket)
        await mark_dirty(db, {(ASYNC, _day(YESTERDAY))}, now=NOW)
        await db.commit()

    source_rows = 0
    original_partitions = AsyncResult.partitions

    async def count_transported_rows(result, *args, **kwargs):
        nonlocal source_rows
        async for batch in original_partitions(result, *args, **kwargs):
            source_rows += len(batch)
            yield batch

    monkeypatch.setattr(AsyncResult, "partitions", count_transported_rows)
    result = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
    assert result["repaired"] == 1 and result["repair_failed"] == result["repair_pending"] == 0
    assert source_rows == len(evidence)
    bucket = await _bucket()
    assert (bucket.n, bucket.ok, bucket.bad) == (6, 3, 2)
    assert (bucket.hits, bucket.hit_decided, bucket.paid_hits, bucket.free_misses) == (2, 4, 1, 0)
    assert bucket.last_ok_at == YESTERDAY + timedelta(minutes=minute - 1)
    assert bucket.latency_seen == 3 and sorted(bucket.latency_sample) == [100, 400, 500]
    assert bucket.verdicts == {"valid": 1, "not_found": 1, "unknown": 1, "catch_all": 1}
    assert bucket.terminal_reconciled and await _dirty() == []


async def test_recount_of_an_all_refused_day_streams_no_rows_and_clears_stale_evidence(
    clients, monkeypatch,
):
    for refusal in ("balance", "", "policy"):
        await _record(ASYNC, refused_by=refusal, hit=True, verdict="refused")
    folded = await catalog_stats.refresh(session_maker, now=NOW)
    assert folded["rows"] == 3 and folded["caught_up"]
    assert await _bucket() is None
    async with session_maker() as db:
        db.add(EndpointDayStat(
            endpoint_id=ASYNC, day=_day(YESTERDAY), n=9, ok=8, bad=1,
            hits=7, hit_decided=8, paid_hits=2, free_misses=1, last_ok_at=YESTERDAY,
            latency_seen=8, latency_sample=[999], verdicts={"stale": 9},
            terminal_reconciled=False))
        await mark_dirty(db, {(ASYNC, _day(YESTERDAY))}, now=NOW)
        await db.commit()

    source_rows = 0
    original_partitions = AsyncResult.partitions

    async def count_transported_rows(result, *args, **kwargs):
        nonlocal source_rows
        async for batch in original_partitions(result, *args, **kwargs):
            source_rows += len(batch)
            yield batch

    monkeypatch.setattr(AsyncResult, "partitions", count_transported_rows)
    result = await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
    assert result["repaired"] == 1 and result["repair_failed"] == result["repair_pending"] == 0
    assert source_rows == 0
    bucket = await _bucket()
    assert (bucket.n, bucket.ok, bucket.bad, bucket.hits, bucket.hit_decided,
            bucket.paid_hits, bucket.free_misses, bucket.latency_seen) == (0,) * 8
    assert bucket.last_ok_at is None and bucket.latency_sample == [] and bucket.verdicts == {}
    assert bucket.terminal_reconciled and bucket.updated_at == NOW and await _dirty() == []
