"""The pool gauge is the reading behind `TREG_DB_POOL_OVERRIDES`: per-minute peak checked-out
connections per pool, next to capacity. Sizing by arithmetic got both minor pools wrong once each
(ops/deploy.md § Database pools); this is what settles the number."""
import asyncio

import pytest

from treg import analytics, bootstrap
from treg.infra import db as infra_db


def test_snapshot_reports_nothing_on_sqlite_and_a_pool_row_per_engine_otherwise():
    snap = infra_db.pool_snapshot()
    if infra_db._is_sqlite:
        assert snap == {}
        return
    assert set(snap) == {"api", "admin", "background"}
    for name, row in snap.items():
        spec = infra_db.POOL_SPECS[name]
        assert row["capacity"] == spec["pool_size"] + spec["max_overflow"]
        assert 0 <= row["checked_out"] <= row["capacity"]


async def test_gauge_emits_one_event_per_window_with_peak_capacity_and_headroom(monkeypatch):
    samples = iter([
        {"api": {"checked_out": 4, "capacity": 15}, "background": {"checked_out": 1, "capacity": 13}},
        {"api": {"checked_out": 11, "capacity": 15}, "background": {"checked_out": 13, "capacity": 13}},
        {"api": {"checked_out": 2, "capacity": 15}, "background": {"checked_out": 0, "capacity": 13}},
    ])
    last = {"api": {"checked_out": 0, "capacity": 15}, "background": {"checked_out": 0, "capacity": 13}}
    monkeypatch.setattr(infra_db, "pool_snapshot", lambda: next(samples, last))
    captured: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(analytics, "capture", lambda d, e, p=None, **kw: captured.append((d, e, p)))
    task = asyncio.create_task(bootstrap.pool_gauge(sample_s=0.01, emit_s=0.05))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if captured:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert captured, "no gauge event within the window"
    distinct_id, event, props = captured[0]
    assert (distinct_id, event) == (analytics.SERVER_DISTINCT_ID, "db_pool_gauge")
    assert props["api_peak"] == 11 and props["api_capacity"] == 15 and props["api_headroom"] == 4
    assert props["background_peak"] == 13 and props["background_headroom"] == 0
    assert props["samples"] >= 3
