"""Quota outage telemetry is bounded by time, not call or target volume."""

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from treg import analytics


EVENT = "org_endpoint_quota_fail_open"
TARGET = (42, "example.read")


@pytest.fixture(autouse=True)
async def telemetry(monkeypatch):
    now = [100.0]
    posts = []

    async def post(batch):
        posts.extend(batch)

    monkeypatch.setattr(analytics, "enabled", lambda: True)
    monkeypatch.setattr(analytics, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(analytics, "_quota_window", None, raising=False)
    monkeypatch.setattr(analytics, "_queue", [])
    monkeypatch.setattr(analytics, "_fault_windows", OrderedDict())
    monkeypatch.setattr(analytics, "_flusher", None)
    monkeypatch.setattr(analytics, "_post", post)
    yield now, posts
    await analytics.drain()


def quota_events():
    return [event for event in analytics._queue if event["event"] == EVENT]


def test_burst_rolls_up_all_admissions_and_target_entries(telemetry):
    now, _ = telemetry
    for _ in range(1000):
        analytics.capture_quota_fail_open((TARGET, (42, "example.child")))
    assert analytics._queue == []
    now[0] += 59
    analytics._emit_quota_summary()
    assert analytics._queue == []
    now[0] += 1
    analytics._emit_quota_summary()
    (event,) = quota_events()
    assert event["distinct_id"] == analytics.SERVER_DISTINCT_ID
    assert event["properties"] == {
        "admissions": 1000,
        "target_count": 2000,
        "target_examples": [
            {"org_id": 42, "endpoint_id": "example.read"},
            {"org_id": 42, "endpoint_id": "example.child"},
        ],
        "targets_truncated": False,
        "$lib": "treg-server",
        "build": analytics.build_id(),
        "archive_config": analytics.archive_config_id(),
    }
    assert analytics._quota_window is None
    analytics._emit_quota_summary(force=True)
    assert len(quota_events()) == 1


def test_target_examples_bound_distinct_identities_without_losing_totals(telemetry):
    for i in range(1000):
        analytics.capture_quota_fail_open(((i + 1, "example.read"),))
    for _ in range(10):
        analytics.capture_quota_fail_open(((1, "example.read"),))
    assert len(analytics._quota_window.examples) == 10
    analytics._emit_quota_summary(force=True)
    props = quota_events()[0]["properties"]
    assert props["admissions"] == props["target_count"] == 1010
    assert props["target_examples"] == [
        {"org_id": i + 1, "endpoint_id": "example.read"} for i in range(10)
    ]
    assert props["targets_truncated"] is True


def test_repeating_the_ten_examples_does_not_mark_them_truncated(telemetry):
    targets = tuple((i + 1, "example.read") for i in range(10))
    for _ in range(100):
        analytics.capture_quota_fail_open(targets)
    analytics._emit_quota_summary(force=True)
    assert quota_events()[0]["properties"]["targets_truncated"] is False


def test_later_window_has_only_new_counts_and_examples(telemetry):
    now, _ = telemetry
    analytics.capture_quota_fail_open((TARGET,))
    now[0] += 60
    analytics._emit_quota_summary()
    analytics.capture_quota_fail_open(((43, "example.other"),))
    now[0] += 59
    analytics._emit_quota_summary()
    assert len(quota_events()) == 1
    now[0] += 1
    analytics._emit_quota_summary()
    props = quota_events()[1]["properties"]
    assert props["admissions"] == props["target_count"] == 1
    assert props["target_examples"] == [{"org_id": 43, "endpoint_id": "example.other"}]


async def test_timer_emits_after_calls_stop_with_no_queued_events(telemetry, monkeypatch):
    now, posts = telemetry
    ticks = asyncio.Queue()
    sent = asyncio.Event()

    async def tick(delay):
        assert delay == analytics._FLUSH_INTERVAL_S
        now[0] += await ticks.get()

    async def post(batch):
        posts.extend(batch)
        sent.set()

    monkeypatch.setattr(analytics.asyncio, "sleep", tick)
    monkeypatch.setattr(analytics, "_post", post)
    analytics.capture_quota_fail_open((TARGET,))
    assert analytics._queue == []
    ticks.put_nowait(60)
    await asyncio.wait_for(sent.wait(), timeout=1)
    await analytics._flusher
    assert [event["event"] for event in posts] == [EVENT]
    assert posts[0]["properties"]["admissions"] == 1
    assert analytics._quota_window is None


async def test_shutdown_emits_partial_window_once(telemetry):
    _, posts = telemetry
    analytics.capture_quota_fail_open((TARGET,))
    analytics.capture_quota_fail_open((TARGET,))
    await analytics.drain()
    await analytics.drain()
    assert len(posts) == 1
    assert posts[0]["properties"]["admissions"] == 2
    assert analytics._quota_window is None


async def test_capture_reuses_flusher_and_restarts_a_cancelled_task(telemetry):
    _, posts = telemetry
    analytics.capture_quota_fail_open((TARGET,))
    first_flusher = analytics._flusher
    for _ in range(100):
        analytics.capture_quota_fail_open((TARGET,))
        assert analytics._flusher is first_flusher
    first_flusher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_flusher
    analytics.capture_quota_fail_open((TARGET,))
    assert analytics._flusher is not first_flusher
    await analytics.drain()
    assert len(posts) == 1
    assert posts[0]["properties"]["admissions"] == 102


def test_disabled_and_empty_admissions_leave_no_state(telemetry, monkeypatch):
    analytics.capture_quota_fail_open(())
    monkeypatch.setattr(analytics, "enabled", lambda: False)
    analytics.capture_quota_fail_open((TARGET,))
    assert analytics._queue == []
    assert analytics._quota_window is None
    assert analytics._flusher is None


def test_full_queue_drops_summary_without_retaining_unbounded_retry_state(telemetry, monkeypatch):
    monkeypatch.setattr(analytics, "_MAX_PENDING", 1)
    analytics.capture(analytics.SERVER_DISTINCT_ID, "existing")
    analytics.capture_quota_fail_open((TARGET,))
    analytics._emit_quota_summary(force=True)
    assert [event["event"] for event in analytics._queue] == ["existing"]
    assert analytics._quota_window is None
    analytics._queue.clear()
    analytics._emit_quota_summary(force=True)
    assert analytics._queue == []


async def test_failed_delivery_drops_summary_and_drain_does_not_retry(telemetry, monkeypatch):
    attempts = []

    async def post(batch):
        attempts.append(batch)
        raise RuntimeError("synthetic telemetry sink failure")

    monkeypatch.setattr(analytics, "_post", post)
    analytics.capture_quota_fail_open((TARGET,))
    await analytics.drain()
    await analytics.drain()
    assert len(attempts) == 1
    assert analytics._quota_window is None
    assert analytics._queue == []


def test_capture_does_not_propagate_observer_failure(telemetry, monkeypatch):
    def broken():
        raise RuntimeError("synthetic observer failure")

    monkeypatch.setattr(analytics, "_ensure_flusher", broken)
    analytics.capture_quota_fail_open((TARGET,))
    monkeypatch.setattr(analytics, "enabled", broken)
    analytics.capture_quota_fail_open((TARGET,))


def test_concurrent_capture_preserves_counts_and_memory_bound(telemetry):
    def record(org_id):
        for _ in range(100):
            analytics.capture_quota_fail_open(((org_id, "example.read"),))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(1, 25)))
    assert len(analytics._quota_window.examples) == 10
    analytics._emit_quota_summary(force=True)
    props = quota_events()[0]["properties"]
    assert props["admissions"] == props["target_count"] == 2400
    assert props["targets_truncated"] is True
