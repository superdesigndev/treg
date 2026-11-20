"""Incremental, content-free observations of real Web provider calls.

The cursor walks audit ids once. Daily buckets keep the public read off the large audit table;
only actual uncached provider attempts enter these numbers. Quality stays in WebArenaRun.
"""
from __future__ import annotations

import random
import time
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from ..infra.db import session_maker
from ..models import (CallRecord, WebArenaCallCursor, WebArenaCallDayStat,
                      WebArenaSeedDayStat, WebArenaSeedProgress)
from ..timeutil import utcnow_naive as now
from . import catalog_stats

WINDOW_DAYS = 30
LAG = timedelta(seconds=60)
BATCH_ROWS = 5_000
LATENCY_SAMPLE = 400
MIN_HIT_SAMPLES = 20
MIN_TIME_SAMPLES = 20
CURSOR_ID = "callrecord"
SEED_DAYS = 10
SEED_MAX_ROWS = 250_000
SEED_MAX_SECONDS = 110
CALL_COLUMNS = (CallRecord.id, CallRecord.endpoint_id, CallRecord.created_at,
                CallRecord.kind, CallRecord.cached, CallRecord.refused_by,
                CallRecord.hit, CallRecord.status_code, CallRecord.duration_ms,
                CallRecord.response_bytes)


def endpoint_tasks() -> dict[str, tuple[str, str]]:
    """Only the endpoints actually offered by the Web Arena task lineup."""
    from .web_arena import tasks

    return {entry["endpoint_id"]: (task["id"], entry["provider"])
            for task in tasks(_internal=True) if task["enabled"] for entry in task["provider_previews"]}


def eligible(row) -> bool:
    return (row.kind == "call" and row.endpoint_id and not row.cached
            and row.refused_by is None
            # A relayed 502 has a buffered body size, even when its body is empty.
            # A treg gateway failure has no upstream response and no body size.
            and not (row.status_code == 502 and row.response_bytes is None))


def fold(bucket: dict, row, *, rng=None) -> None:
    """Unknown outcomes stay unknown; provider faults count as no usable result."""
    bucket["calls"] += 1
    if row.hit is not None and (row.status_code < 400 or row.status_code >= 500 or row.status_code == 405):
        bucket["decided"] += 1
        bucket["hits"] += row.hit is True
    elif row.status_code >= 500 or row.status_code == 405:
        bucket["decided"] += 1
    if 200 <= row.status_code < 300 and row.duration_ms is not None:
        bucket["timed"] += 1
        bucket["duration_sum_ms"] += row.duration_ms
        sample = bucket["duration_sample"]
        if len(sample) < LATENCY_SAMPLE:
            sample.append(row.duration_ms)
        else:
            slot = (rng or random).randrange(bucket["timed"])
            if slot < LATENCY_SAMPLE:
                sample[slot] = row.duration_ms


def _new_bucket() -> dict:
    return {"calls": 0, "decided": 0, "hits": 0, "timed": 0,
            "duration_sum_ms": 0, "duration_sample": []}


def _bucket_from(row: WebArenaCallDayStat) -> dict:
    return {"calls": row.calls, "decided": row.decided, "hits": row.hits,
            "timed": row.timed, "duration_sum_ms": row.duration_sum_ms,
            "duration_sample": list(row.duration_sample or [])}


async def _seed_progress(db) -> WebArenaSeedProgress:
    """Lock the saved seed position so two shell commands cannot fold the same rows."""
    return (await db.execute(select(WebArenaSeedProgress).where(
        WebArenaSeedProgress.id == "recent").with_for_update())).scalar_one()


async def start_recent_seed(session_factory=session_maker, *, at: datetime | None = None) -> None:
    """Freeze the ten-day range once; an interrupted command resumes that same range."""
    at = at or now()
    async with session_factory() as db:
        cursor = await db.get(WebArenaCallCursor, CURSOR_ID)
        if cursor and cursor.observed_since is not None:
            raise ValueError("Web Arena has already been seeded; no totals were changed.")
        progress = await db.get(WebArenaSeedProgress, "recent")
        if progress:
            return
        since, lagged = at - timedelta(days=SEED_DAYS), at - LAG
        first_id = await catalog_stats._first_id_at(db, since) - 1
        highwater_id = await catalog_stats._first_id_at(db, lagged) - 1
        insert = catalog_stats._insert(db)
        await db.execute(insert(WebArenaSeedProgress).values(
            id="recent", observed_since=since, lagged_until=lagged,
            first_id=first_id, highwater_id=highwater_id,
            endpoints=list(endpoint_tasks()), last_id=first_id, endpoint_index=0,
            scanned=0, eligible_calls=0, updated_at=at)
            .on_conflict_do_nothing(index_elements=["id"]))
        await db.commit()


async def advance_recent_seed(session_factory=session_maker, *, max_rows: int = SEED_MAX_ROWS,
                              page_rows: int = BATCH_ROWS, max_seconds: int = SEED_MAX_SECONDS) -> dict:
    """Fold a bounded batch into separate aggregate staging rows and commit each page."""
    consumed = 0
    started = time.monotonic()
    while consumed < max_rows and time.monotonic() - started < max_seconds:
        async with session_factory() as db:
            progress = await _seed_progress(db)
            if progress.endpoint_index >= len(progress.endpoints):
                return {"ready": True, "rows_this_run": consumed,
                        "scanned": progress.scanned, "eligible_calls": progress.eligible_calls}
            endpoint_id = progress.endpoints[progress.endpoint_index]
            limit = min(page_rows, max_rows - consumed)
            rows = (await db.execute(select(*CALL_COLUMNS).where(
                CallRecord.endpoint_id == endpoint_id, CallRecord.id > progress.last_id,
                CallRecord.id <= progress.highwater_id,
                CallRecord.created_at >= progress.observed_since,
                CallRecord.created_at < progress.lagged_until)
                .order_by(CallRecord.id).limit(limit))).all()
            if not rows:
                progress.endpoint_index += 1
                progress.last_id = progress.first_id
            else:
                touched: dict[tuple[str, str], dict] = {}
                for row in rows:
                    if not eligible(row):
                        continue
                    key = row.endpoint_id, row.created_at.strftime("%Y-%m-%d")
                    if key not in touched:
                        existing = (await db.execute(select(WebArenaSeedDayStat).where(
                            WebArenaSeedDayStat.endpoint_id == key[0],
                            WebArenaSeedDayStat.day == key[1]))).scalar_one_or_none()
                        touched[key] = _bucket_from(existing) if existing else _new_bucket()
                    fold(touched[key], row)
                insert = catalog_stats._insert(db)
                for (endpoint, day), values in touched.items():
                    stmt = insert(WebArenaSeedDayStat).values(endpoint_id=endpoint, day=day, **values)
                    await db.execute(stmt.on_conflict_do_update(
                        index_elements=["endpoint_id", "day"],
                        set_={field: getattr(stmt.excluded, field) for field in values}))
                progress.last_id = rows[-1].id
                progress.scanned += len(rows)
                progress.eligible_calls += sum(eligible(row) for row in rows)
                if len(rows) < limit:
                    progress.endpoint_index += 1
                    progress.last_id = progress.first_id
            progress.updated_at = now()
            db.add(progress)
            await db.commit()
            consumed += len(rows)
    return {"rows_this_run": consumed, **await recent_seed_status(session_factory)}


async def recent_seed_status(session_factory=session_maker) -> dict:
    async with session_factory() as db:
        progress = await db.get(WebArenaSeedProgress, "recent")
        if progress is None:
            return {"ready": False}
        return {"ready": progress.endpoint_index >= len(progress.endpoints),
                "scanned": progress.scanned, "eligible_calls": progress.eligible_calls,
                "endpoint_index": progress.endpoint_index,
                "endpoint_count": len(progress.endpoints),
                "observed_since": progress.observed_since.isoformat() + "Z"}


async def finish_recent_seed(db) -> dict:
    """Atomically replace partial live totals only after all staged endpoints are complete."""
    progress = await _seed_progress(db)
    if progress.endpoint_index < len(progress.endpoints):
        raise ValueError("Web Arena seed is not complete; saved totals were not changed.")
    insert = catalog_stats._insert(db)
    await db.execute(insert(WebArenaCallCursor).values(
        id=CURSOR_ID, call_id=-1, updated_at=now()).on_conflict_do_nothing(index_elements=["id"]))
    cursor = (await db.execute(select(WebArenaCallCursor).where(
        WebArenaCallCursor.id == CURSOR_ID).with_for_update())).scalar_one()
    if cursor.observed_since is not None:
        raise ValueError("Web Arena has already been seeded; no totals were changed.")
    await db.execute(delete(WebArenaCallDayStat))
    staged = (await db.execute(select(WebArenaSeedDayStat))).scalars().all()
    for row in staged:
        db.add(WebArenaCallDayStat(endpoint_id=row.endpoint_id, day=row.day,
                                  **_bucket_from(row)))
    cursor.call_id = progress.highwater_id
    cursor.observed_since = progress.observed_since
    cursor.updated_at = now()
    db.add(cursor)
    result = {"observed_since": progress.observed_since.isoformat() + "Z",
              "scanned": progress.scanned, "eligible_calls": progress.eligible_calls,
              "daily_rows": len(staged), "cursor": progress.highwater_id}
    await db.execute(delete(WebArenaSeedDayStat))
    await db.delete(progress)
    return result


async def coverage_start(session_factory=session_maker) -> datetime | None:
    async with session_factory() as db:
        cursor = await db.get(WebArenaCallCursor, CURSOR_ID)
        return cursor.observed_since if cursor else None


async def seeded(session_factory=session_maker) -> bool:
    return await coverage_start(session_factory) is not None


async def collect(session_factory=session_maker, *, max_rows: int = 50_000,
                  batch_rows: int = BATCH_ROWS) -> dict:
    """Process recent audit records under a locked cursor; stop before young inserts."""
    at = now()
    accepted = endpoint_tasks()
    consumed = 0
    caught_up = False
    while consumed < max_rows and not caught_up:
        async with session_factory() as db:
            insert = catalog_stats._insert(db)
            since = at - timedelta(days=WINDOW_DAYS)
            await db.execute(insert(WebArenaCallCursor).values(
                id=CURSOR_ID, call_id=-1, updated_at=at)
                .on_conflict_do_nothing(index_elements=["id"]))
            cursor = (await db.execute(select(WebArenaCallCursor)
                      .where(WebArenaCallCursor.id == CURSOR_ID).with_for_update())).scalar_one()
            if cursor.call_id == -1:
                cursor.call_id = await catalog_stats._first_id_at(db, since) - 1
            limit = min(batch_rows, max_rows - consumed)
            rows = (await db.execute(select(*CALL_COLUMNS).where(CallRecord.id > cursor.call_id)
                    .order_by(CallRecord.id).limit(limit))).all()
            touched: dict[tuple[str, str], dict] = {}
            last_id = cursor.call_id
            deferred = False
            for row in rows:
                if row.created_at >= at - LAG:
                    deferred = True
                    break
                last_id = row.id
                consumed += 1
                if row.created_at < since or row.endpoint_id not in accepted or not eligible(row):
                    continue
                key = row.endpoint_id, row.created_at.strftime("%Y-%m-%d")
                if key not in touched:
                    existing = (await db.execute(select(WebArenaCallDayStat).where(
                        WebArenaCallDayStat.endpoint_id == key[0],
                        WebArenaCallDayStat.day == key[1]))).scalar_one_or_none()
                    touched[key] = _bucket_from(existing) if existing else _new_bucket()
                fold(touched[key], row)
            for (endpoint_id, day), values in touched.items():
                stmt = insert(WebArenaCallDayStat).values(endpoint_id=endpoint_id, day=day, **values)
                await db.execute(stmt.on_conflict_do_update(
                    index_elements=["endpoint_id", "day"],
                    set_={field: getattr(stmt.excluded, field) for field in values}))
            cursor.call_id = last_id
            cursor.updated_at = at
            db.add(cursor)
            if deferred or len(rows) < limit:
                caught_up = True
                await db.execute(delete(WebArenaCallDayStat).where(
                    WebArenaCallDayStat.day < since.strftime("%Y-%m-%d")))
            await db.commit()
            if not rows:
                break
    return {"rows": consumed, "caught_up": caught_up}


def _weighted_median(values: list[tuple[int, float]]) -> int | None:
    if not values:
        return None
    total = sum(weight for _, weight in values)
    passed = 0.0
    for value, weight in sorted(values):
        passed += weight
        if passed >= total / 2:
            return value
    return values[-1][0]


def summarize(buckets: dict[str, list[dict]]) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, dict]] = defaultdict(dict)
    for endpoint_id, (task, provider) in endpoint_tasks().items():
        days = buckets.get(endpoint_id, [])
        calls = sum(day["calls"] for day in days)
        decided = sum(day["decided"] for day in days)
        hits = sum(day["hits"] for day in days)
        timed = sum(day["timed"] for day in days)
        duration_sum = sum(day["duration_sum_ms"] for day in days)
        weighted = [(ms, day["timed"] / len(day["duration_sample"]))
                    for day in days if day["duration_sample"] for ms in day["duration_sample"]]
        # `decided` includes provider faults while `timed` counts successful responses.
        # If even that larger decided count is less than half the successful calls,
        # most successes provably lack a hit verdict. Do not rank a provider from
        # faults accumulated before its result rule started recording verdicts.
        incomplete_verdicts = decided * 2 < timed
        result[task][provider] = {
            "runs": calls, "hit_samples": decided,
            "success_rate": round(100 * hits / decided, 1)
            if decided >= MIN_HIT_SAMPLES and not incomplete_verdicts else None,
            "average_provider_ms": round(duration_sum / timed) if timed >= MIN_TIME_SAMPLES else None,
            "median_provider_ms": _weighted_median(weighted) if timed >= MIN_TIME_SAMPLES else None,
            "time_samples": timed,
        }
    return result


async def snapshot(session_factory=session_maker) -> dict[str, dict[str, dict]]:
    since = (now() - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    async with session_factory() as db:
        rows = (await db.execute(select(WebArenaCallDayStat).where(WebArenaCallDayStat.day >= since))).scalars().all()
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[row.endpoint_id].append(_bucket_from(row))
    return summarize(buckets)


async def local_snapshot(session_factory=session_maker) -> dict[str, dict[str, dict]]:
    """Development reads recent calls directly, so a cron is not needed to preview a run."""
    ids = list(endpoint_tasks())
    if not ids:
        return {}
    since = now() - timedelta(days=WINDOW_DAYS)
    observed_since = await coverage_start(session_factory)
    if observed_since is not None:
        since = max(since, observed_since)
    async with session_factory() as db:
        rows = (await db.execute(select(*CALL_COLUMNS).where(
            CallRecord.endpoint_id.in_(ids), CallRecord.created_at >= since,
            CallRecord.kind == "call", CallRecord.cached.is_(False),
            CallRecord.refused_by.is_(None)))).all()
    buckets: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        if not eligible(row):
            continue
        day = row.created_at.strftime("%Y-%m-%d")
        bucket = buckets[row.endpoint_id].setdefault(day, _new_bucket())
        fold(bucket, row)
    return summarize({endpoint: list(days.values()) for endpoint, days in buckets.items()})
