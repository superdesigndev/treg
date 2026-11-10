"""A one-off async recount records its exact targets and proves completion from durable state."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from treg.application import catalog_stats
from treg.domain.catalog import stats
from treg.domain.catalog import store as catalog_store
from treg.infra import catalog_stat_dirty
from treg.infra.db import session_maker
from treg.models import CallRecord, EndpointDayStat, EndpointStatCursor, EndpointStatDirty

NOW = datetime(2026, 10, 15, 12)
SUBMITTED = datetime(2026, 10, 14, 23)
DAY = SUBMITTED.strftime("%Y-%m-%d")
ASYNC = "wiza.people.email.find"
SYNC = "tikhub.tiktok.user.profile"


def _day(at: datetime) -> str:
    return at.strftime("%Y-%m-%d")


def _manifest(keys, *, applied=True, at=NOW):
    return {
        "kind": "catalog-async-repair", "version": 1, "applied": applied,
        "requested_at": at.isoformat(), "window_start": stats.window_days(at),
        "window_end": _day(at), "target_count": len(keys),
        "targets": [{"endpoint_id": endpoint, "day": day} for endpoint, day in sorted(keys)],
    }


async def _seed_buckets(keys, *, cursor=True):
    async with session_maker() as db:
        if cursor:
            db.add(EndpointStatCursor(id=catalog_stats.CURSOR_ID, cursor_id=0,
                                      caught_up_at=NOW, updated_at=NOW))
        for endpoint, day in keys:
            db.add(EndpointDayStat(
                endpoint_id=endpoint, day=day, n=9, ok=5, bad=3,
                hits=3, hit_decided=7, paid_hits=2, free_misses=1,
                last_ok_at=SUBMITTED, latency_seen=5, latency_sample=[10, 20, 30, 40, 50],
                verdicts={"valid": 3, "not_found": 4}, updated_at=NOW - timedelta(minutes=7),
                terminal_reconciled=True))
        await db.commit()


async def _buckets():
    async with session_maker() as db:
        rows = (await db.execute(select(EndpointDayStat).order_by(
            EndpointDayStat.endpoint_id, EndpointDayStat.day))).scalars()
        return [row.model_dump() for row in rows]


async def _markers():
    async with session_maker() as db:
        rows = (await db.execute(select(EndpointStatDirty).order_by(
            EndpointStatDirty.endpoint_id, EndpointStatDirty.day))).scalars()
        return [row.model_dump() for row in rows]


async def _record(*, hit=True, at=SUBMITTED):
    async with session_maker() as db:
        row = CallRecord(org_id=1, user_email="synthetic@example.test", tool_name=ASYNC,
                         method="POST", path="/synthetic", endpoint_id=ASYNC,
                         status_code=200, duration_ms=100, hit=hit,
                         verdict="valid" if hit else "not_found", created_at=at)
        db.add(row)
        await db.commit()
        return row.id


@contextmanager
def _statements():
    captured = []
    engine = session_maker.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield captured
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def test_default_dry_run_selects_only_current_mutable_buckets_without_writing(clients, monkeypatch):
    catalog = catalog_store.load()
    monkeypatch.setitem(catalog.adapters, ASYNC, replace(catalog.adapters[ASYNC], verified=False))
    another = "test.async.requeue"
    no_adapter = "test.async.no_adapter"
    for endpoint in (another, no_adapter):
        monkeypatch.setitem(catalog.by_id, endpoint, {"id": endpoint, "async": {"state": "status"}})
    monkeypatch.setitem(catalog.adapters, another, catalog.adapters[ASYNC])
    first = stats.window_days(NOW)
    old = _day(datetime.fromisoformat(first) - timedelta(days=1))
    future = _day(NOW + timedelta(days=1))
    targets = [(ASYNC, first), (ASYNC, _day(NOW)), (another, DAY)]
    await _seed_buckets(targets + [(ASYNC, old), (ASYNC, future), (SYNC, DAY), (no_adapter, DAY)],
                        cursor=False)
    before = await _buckets()

    with _statements() as sql:
        manifest = await catalog_stats.requeue_async_stats(session_maker, now=NOW)
    assert manifest == _manifest(targets, applied=False)
    assert await _buckets() == before and await _markers() == []
    assert not any(statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
                   for statement in sql)
    assert not any("callrecord" in statement.lower() for statement in sql)


async def test_apply_preserves_tallies_and_existing_marker_age_and_backoff(clients):
    key = (ASYNC, DAY)
    await _seed_buckets([key, (SYNC, DAY)])
    queued = NOW - timedelta(hours=2)
    retry = NOW + timedelta(hours=1)
    async with session_maker() as db:
        db.add(EndpointStatDirty(endpoint_id=ASYNC, day=DAY, revision=7,
                                 queued_at=queued, retry_at=retry))
        await db.commit()
    before = await _buckets()

    manifest = await catalog_stats.requeue_async_stats(session_maker, apply=True, now=NOW)
    assert manifest == _manifest([key])
    after = await _buckets()
    for old, new in zip(before, after):
        assert {name: value for name, value in new.items() if name != "terminal_reconciled"} == {
            name: value for name, value in old.items() if name != "terminal_reconciled"}
        assert new["terminal_reconciled"] is (new["endpoint_id"] != ASYNC)
    marker, = await _markers()
    assert (marker["revision"], marker["queued_at"], marker["retry_at"]) == (8, queued, retry)


async def test_apply_rolls_back_flags_and_markers_if_queue_write_fails(clients, monkeypatch):
    await _seed_buckets([(ASYNC, DAY), (ASYNC, _day(NOW))])
    async with session_maker() as db:
        await catalog_stat_dirty.mark_dirty(db, [(ASYNC, DAY)], now=NOW - timedelta(hours=1))
        await db.commit()
    before, markers_before = await _buckets(), await _markers()
    original = catalog_stat_dirty.mark_dirty

    async def fail_after_marking(db, keys, *, now=None):
        await original(db, keys, now=now)
        raise RuntimeError("synthetic requeue failure before commit")

    monkeypatch.setattr(catalog_stat_dirty, "mark_dirty", fail_after_marking)
    with pytest.raises(RuntimeError, match="synthetic requeue failure"):
        await catalog_stats.requeue_async_stats(session_maker, apply=True, now=NOW)
    assert await _buckets() == before
    assert await _markers() == markers_before


async def test_apply_requires_a_fold_cursor_and_leaves_the_database_unchanged(clients):
    await _seed_buckets([(ASYNC, DAY)], cursor=False)
    before = await _buckets()
    with pytest.raises(ValueError):
        await catalog_stats.requeue_async_stats(session_maker, apply=True, now=NOW)
    assert await _buckets() == before and await _markers() == []


async def test_append_does_not_complete_requeue_but_full_repair_does(clients, monkeypatch):
    monkeypatch.setattr(catalog_store, "empty_is_failure", lambda _: False)
    row_id = await _record()
    await catalog_stats.refresh(session_maker, now=NOW)
    async with session_maker() as db:
        # Model an old writer that corrected the audit row without marking the folded day.
        row = await db.get(CallRecord, row_id)
        row.hit, row.verdict = False, "not_found"
        db.add(row)
        await db.commit()
    manifest = await catalog_stats.requeue_async_stats(session_maker, apply=True, now=NOW)
    status = await catalog_stats.repair_status(manifest, session_maker, now=NOW)
    assert not status["complete"] and status["pending"] == 1

    await _record(at=SUBMITTED + timedelta(minutes=1))
    await catalog_stats.refresh(session_maker, max_dirty_buckets=0, now=NOW)
    bucket, = await _buckets()
    assert bucket["n"] == 2 and not bucket["terminal_reconciled"]
    assert not (await catalog_stats.repair_status(manifest, session_maker, now=NOW))["complete"]

    await catalog_stats.refresh(session_maker, max_rows=0, now=NOW)
    status = await catalog_stats.repair_status(manifest, session_maker, now=NOW)
    assert status == {"complete": True, "repaired": 1, "pending": 0, "missing": 0, "aged_out": 0,
                      "targets": [{"endpoint_id": ASYNC, "day": DAY, "status": "repaired"}]}
    bucket, = await _buckets()
    assert (bucket["n"], bucket["hits"], bucket["hit_decided"]) == (2, 1, 2)
    assert bucket["verdicts"] == {"valid": 1, "not_found": 1}


async def test_status_requires_reconciled_flag_and_no_dirty_marker_in_one_read(clients):
    pending_day, dirty_day, repaired_day = stats.window_days(NOW), DAY, _day(NOW)
    await _seed_buckets([(ASYNC, day) for day in (pending_day, dirty_day, repaired_day)])
    async with session_maker() as db:
        bucket = await db.get(EndpointDayStat, (ASYNC, pending_day))
        bucket.terminal_reconciled = False
        db.add(bucket)
        await catalog_stat_dirty.mark_dirty(db, [(ASYNC, dirty_day)], now=NOW)
        await db.commit()
    manifest = _manifest([(ASYNC, day) for day in (pending_day, dirty_day, repaired_day)])
    with _statements() as sql:
        status = await catalog_stats.repair_status(manifest, session_maker, now=NOW)
    assert not status["complete"] and status["pending"] == 2 and status["repaired"] == 1
    assert status["missing"] == status["aged_out"] == 0
    assert [target["status"] for target in status["targets"]] == ["pending", "pending", "repaired"]
    reads = [statement for statement in sql if statement.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(reads) == 1
    assert "endpointdaystat" in reads[0].lower() and "endpointstatdirty" in reads[0].lower()
    assert not any("callrecord" in statement.lower() for statement in sql)


async def test_status_marks_missing_buckets_incomplete_and_aged_out_targets_nonblocking(clients):
    first, today = stats.window_days(NOW), _day(NOW)
    await _seed_buckets([(ASYNC, today)])
    missing = _manifest([(ASYNC, first), (ASYNC, today)])
    status = await catalog_stats.repair_status(missing, session_maker, now=NOW)
    assert not status["complete"] and status["missing"] == 1 and status["repaired"] == 1
    assert status["targets"][0] == {"endpoint_id": ASYNC, "day": first, "status": "missing"}

    later = await catalog_stats.repair_status(missing, session_maker, now=NOW + timedelta(days=1))
    assert later["complete"] and later["aged_out"] == 1 and later["repaired"] == 1
    assert later["missing"] == later["pending"] == 0
    assert later["targets"][0]["status"] == "aged_out"


async def test_repeated_apply_requeues_one_marker_per_target_without_changing_evidence(clients):
    keys = [(ASYNC, DAY), (ASYNC, _day(NOW))]
    await _seed_buckets(keys)
    first = await catalog_stats.requeue_async_stats(session_maker, apply=True, now=NOW)
    after_first = await _buckets()
    second = await catalog_stats.requeue_async_stats(session_maker, apply=True,
                                                    now=NOW + timedelta(minutes=1))
    assert first["targets"] == second["targets"] and second["target_count"] == 2
    assert await _buckets() == after_first
    markers = await _markers()
    assert len(markers) == 2
    assert all(marker["revision"] == 2 and marker["queued_at"] == marker["retry_at"] == NOW
               for marker in markers)
    assert not (await catalog_stats.repair_status(first, session_maker, now=NOW))["complete"]


async def test_status_uses_reconciliation_state_when_repair_started_before_requeue_time(clients):
    earlier = NOW - timedelta(minutes=10)
    await _record()
    await catalog_stats.refresh(session_maker, now=earlier)
    async with session_maker() as db:
        await catalog_stat_dirty.mark_dirty(db, [(ASYNC, DAY)], now=earlier)
        await db.commit()
    manifest = await catalog_stats.requeue_async_stats(session_maker, apply=True, now=NOW)

    # An already-running worker can acquire the lock after requeue while retaining its older
    # run timestamp. Its successful False -> True transition is the proof, not updated_at.
    result = await catalog_stats.refresh(session_maker, max_rows=0,
                                         now=earlier + timedelta(seconds=1))
    assert result["repaired"] == 1
    bucket, = await _buckets()
    assert bucket["updated_at"] < datetime.fromisoformat(manifest["requested_at"])
    assert bucket["terminal_reconciled"]
    status = await catalog_stats.repair_status(manifest, session_maker, now=NOW)
    assert status["complete"] and status["repaired"] == 1


@pytest.mark.parametrize("change", [
    {"kind": "other-job"}, {"version": 2}, {"applied": False},
    {"target_count": 2}, {"targets": "not-a-target-list"},
    {"requested_at": "not-a-timestamp"}, {"window_start": "not-a-day"},
    {"window_end": "2000-01-01"},
    {"targets": [{"endpoint_id": "", "day": DAY}]},
    {"targets": [{"endpoint_id": ASYNC, "day": "not-a-day"}]},
    {"target_count": 2, "targets": [{"endpoint_id": ASYNC, "day": DAY}] * 2},
])
async def test_status_rejects_unapplied_or_malformed_manifests(change):
    manifest = deepcopy(_manifest([(ASYNC, DAY)]))
    manifest.update(change)
    with pytest.raises(ValueError):
        await catalog_stats.repair_status(manifest, session_maker, now=NOW)


@pytest.mark.parametrize("manifest", [None, [], {}, {"applied": True}])
async def test_status_rejects_missing_manifest_structure(manifest):
    with pytest.raises(ValueError):
        await catalog_stats.repair_status(manifest, session_maker, now=NOW)
