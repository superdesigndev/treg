"""Published-rule parsing, bounded refresh/cache behavior and failure recovery; no services."""
import asyncio
import json
import logging
import time
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from treg.infra import org_flow_control as flow

PRODUCTION_READ_TIMEOUT_S = flow._READ_TIMEOUT_S
STALL_PROOF_READ_TIMEOUT_S = 30.0


@pytest.fixture(autouse=True)
def capture_flow_logs(caplog):
    flow.log.addHandler(caplog.handler)
    try:
        yield
    finally:
        flow.log.removeHandler(caplog.handler)


def payload(org_id=42, version=1, pause_until=None, **config):
    return json.dumps({"org_id": org_id, "version": version, "config": {
        "schema_version": 1, "pause_until": pause_until,
        "rate_limit": None, "concurrency_limit": None, **config}})


@pytest.fixture
def env(monkeypatch):
    clock = [100.0]
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    store = SimpleNamespace(read_org_flow_control=AsyncMock(return_value=None))
    monkeypatch.setattr(flow, "_clock", lambda: clock[0])
    monkeypatch.setattr(flow, "_READ_TIMEOUT_S", STALL_PROOF_READ_TIMEOUT_S)
    monkeypatch.setattr(flow, "_utcnow", lambda: now[0])
    monkeypatch.setattr(flow.kv, "store", lambda: store)
    return flow.Reader(max_entries=3), store, clock, now


@pytest.mark.parametrize("raw", [
    "{", " " * 32769,
    '{"org_id":42,"org_id":42,"version":1,"config":{}}',
    payload(org_id=43), payload(org_id=True), payload(version=True), payload(version=0),
    payload(version=2147483648), payload(schema_version=True), payload(extra=True),
    payload(pause_until="2030-01-01T01:00:00+01:00"),
    payload(pause_until="2030-02-30T00:00:00Z"),
    payload(pause_until="2030-01-01T00:00:00"),
    payload(pause_until="2030-01-01T00:00Z"),
    payload(pause_until="0000-02-30T00:00:00Z"),
    payload(rate_limit={"rps": True, "burst": 1}),
    payload(rate_limit={"rps": 0, "burst": 1}),
    payload(rate_limit={"rps": float("nan"), "burst": 1}),
    payload(rate_limit={"rps": float("inf"), "burst": 1}),
    payload(rate_limit={"rps": 9007199254740992, "burst": 1}),
    payload(rate_limit={"rps": 1, "burst": 1.0}),
    payload(rate_limit={"rps": 1, "burst": 1, "extra": 2}),
    payload(concurrency_limit={"max_in_flight": False}),
    payload(concurrency_limit={"max_in_flight": 0}),
    payload(concurrency_limit={"max_in_flight": 1, "extra": 2}),
    '{"org_id":42,"version":1,"config":{"schema_version":1,"pause_until":null}}',
    '{"org_id":42,"version":1,"config":null}',
])
def test_strict_full_published_envelope(raw):
    with pytest.raises((ValueError, TypeError)):
        flow._parse(raw, 42)


async def test_disabled_guard_never_reads_redis(env, monkeypatch):
    _, store, _, _ = env
    monkeypatch.setattr(flow, "get_settings", lambda: SimpleNamespace(
        org_flow_control_enabled=False))
    assert await flow.check(42, "call-42") is None
    store.read_org_flow_control.assert_not_awaited()


async def test_healthy_missing_is_unconfigured_and_rate_only_snapshot_is_shared(env):
    reader, store, clock, _ = env
    assert await reader.check(42, "first", 2) is None
    store.read_org_flow_control.return_value = payload(
        rate_limit={"rps": 0.01, "burst": 1})
    clock[0] += 3
    snapshot = await reader.check(42, "second", 2)
    assert snapshot.version == 1
    assert snapshot.rate_limit == flow.RateLimit(rps=0.01, burst=1)
    assert snapshot.max_in_flight is None
    assert await reader.check(42, "third", 2) is snapshot
    assert store.read_org_flow_control.await_count == 2


async def test_pause_expires_by_current_utc_without_another_refresh(env):
    reader, store, clock, now = env
    store.read_org_flow_control.return_value = payload(
        pause_until=(now[0] + timedelta(seconds=1.2)).isoformat().replace("+00:00", "Z"),
        rate_limit={"rps": 0.5, "burst": 2},
        concurrency_limit={"max_in_flight": 3})
    with pytest.raises(flow.PauseRejected) as rejected:
        await reader.check(42, "paused", 2)
    assert rejected.value.status_code == 429
    assert rejected.value.retry_after_s == 2
    assert rejected.value.detail["config_version"] == 1
    clock[0] += 1.5
    now[0] += timedelta(seconds=1.5)
    snapshot = reader.entries[42].snapshot
    assert await reader.check(42, "expired", 2) is snapshot
    assert snapshot.max_in_flight == 3
    assert snapshot.rate_limit == flow.RateLimit(rps=0.5, burst=2)
    store.read_org_flow_control.assert_awaited_once_with(42)


async def test_new_version_can_resume_and_conflicts_retain_previous_config(env):
    reader, store, clock, now = env
    store.read_org_flow_control.return_value = payload(
        pause_until=(now[0] + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=2)
    assert (await reader.check(42, None, 2)).version == 2
    for conflicting in [payload(version=1),
                        payload(version=2, rate_limit={"rps": 1, "burst": 1})]:
        clock[0] += 3
        store.read_org_flow_control.return_value = conflicting
        assert (await reader.check(42, None, 2)).version == 2
        assert reader.entries[42].snapshot.version == 2
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=2)
    assert (await reader.check(42, None, 2)).version == 2


@pytest.mark.parametrize("bad", [flow.kv.FlowControlReadFailure.UNAVAILABLE, None])
async def test_transport_failure_or_missing_key_allows_and_recovers(env, bad):
    reader, store, clock, now = env
    original = payload(pause_until=(now[0] + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
    store.read_org_flow_control.return_value = original
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    store.read_org_flow_control.return_value = bad
    # A fresh cached read is valid until its short refresh deadline.
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    clock[0] += 3
    assert await reader.check(42, "failed", 2) is None
    if bad is None:
        assert reader.entries[42].snapshot is None
    else:
        assert reader.entries[42].snapshot.version == 1
    assert await reader.check(42, "backoff", 2) is None
    assert store.read_org_flow_control.await_count == 2
    clock[0] += 3
    store.read_org_flow_control.return_value = original
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, "recovered", 2)


@pytest.mark.parametrize("bad", [flow.kv.FlowControlReadFailure.INVALID, "invalid",
                               payload(org_id=7), payload(version=1),
                               payload(version=2, pause_until="2035-01-01T00:00:00Z")])
async def test_bad_config_preserves_valid_pause_and_original_expiry(env, bad):
    reader, store, clock, now = env
    original = payload(version=2, pause_until=(now[0] + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"))
    store.read_org_flow_control.return_value = original
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    snapshot = reader.entries[42].snapshot
    clock[0] += 3
    store.read_org_flow_control.return_value = bad
    with pytest.raises(flow.PauseRejected) as rejected:
        await reader.check(42, None, 2)
    assert rejected.value.detail["config_version"] == 2
    assert reader.entries[42].snapshot is snapshot
    now[0] += timedelta(seconds=6)
    assert (await reader.check(42, None, 2)).version == 2
    clock[0] += 3
    assert (await reader.check(42, None, 2)).version == 2


@pytest.mark.parametrize("bad", [flow.kv.FlowControlReadFailure.INVALID, "invalid", payload(org_id=7)])
async def test_invalid_config_without_previous_valid_config_allows(env, bad):
    reader, store, _, _ = env
    store.read_org_flow_control.return_value = bad
    assert await reader.check(42, None, 2) is None


async def test_missing_key_clears_rule_before_later_invalid_config(env):
    reader, store, clock, now = env
    store.read_org_flow_control.return_value = payload(pause_until="2035-01-01T00:00:00Z")
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    store.read_org_flow_control.return_value = None
    clock[0] += 3
    assert await reader.check(42, None, 2) is None
    store.read_org_flow_control.return_value = "invalid"
    clock[0] += 3
    assert await reader.check(42, None, 2) is None


async def test_cold_redis_failure_and_initialization_error_are_sanitized(env, monkeypatch):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = flow.kv.FlowControlReadFailure.UNAVAILABLE
    assert await reader.check(42, None, 2) is None
    clock[0] += 3
    def broken_store():
        raise RuntimeError("private-redis-url")
    monkeypatch.setattr(flow.kv, "store", broken_store)
    assert await reader.check(42, None, 2) is None


async def test_concurrent_cold_requests_share_one_read(env):
    reader, store, _, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(_org):
        entered.set()
        await release.wait()
        return payload()
    store.read_org_flow_control.side_effect = read
    tasks = [asyncio.create_task(reader.check(42, str(i), 2)) for i in range(20)]
    await entered.wait()
    assert reader.entries[42].users == 20
    release.set()
    snapshots = await asyncio.gather(*tasks)
    assert all(snapshot is reader.entries[42].snapshot for snapshot in snapshots)
    assert snapshots[0].version == 1
    store.read_org_flow_control.assert_awaited_once_with(42)
    assert reader.entries[42].users == 0


@pytest.mark.parametrize("prior", [None, payload(version=2),
                                 payload(version=2, rate_limit={"rps": 0.5, "burst": 2}),
                                 payload(version=2, pause_until="2035-01-01T00:00:00Z")])
@pytest.mark.parametrize("blocked_on_semaphore", [False, True])
async def test_cancelled_refresh_preserves_shared_state_and_waiter_retries(env, prior, blocked_on_semaphore):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = prior
    try:
        await reader.check(42, None, 2)
    except flow.PauseRejected:
        pass
    entry = reader.entries[42]
    before = (entry.snapshot, entry.available, entry.next_refresh)
    clock[0] += 3
    entered = asyncio.Event()
    async def wait(_org):
        entered.set()
        await asyncio.Event().wait()
    store.read_org_flow_control.side_effect = wait
    if blocked_on_semaphore:
        reader.reads = asyncio.Semaphore(0)
    task = asyncio.create_task(reader.check(42, "cancelled", 2))
    if blocked_on_semaphore:
        await asyncio.sleep(0)
    else:
        await entered.wait()
    time.sleep(0.25)
    waiter = asyncio.create_task(reader.check(42, "waiter", 2))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert entry.users == 1
    assert (entry.snapshot, entry.available, entry.next_refresh) == before
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.entries[42].users == 0
    assert (entry.snapshot, entry.available, entry.next_refresh) == before
    store.read_org_flow_control.side_effect = None
    store.read_org_flow_control.return_value = payload(version=3)
    reader.reads = asyncio.Semaphore(32)
    assert (await reader.check(42, "retry", 2)).version == 3


async def test_cancelled_owner_leaves_waiting_request_free_to_refresh(env):
    reader, store, _, _ = env
    entered = asyncio.Event()

    async def read(_org):
        entered.set()
        await asyncio.Event().wait()

    store.read_org_flow_control.side_effect = read
    owner = asyncio.create_task(reader.check(42, "owner", 2))
    await entered.wait()
    waiter = asyncio.create_task(reader.check(42, "waiter", 2))
    await asyncio.sleep(0)
    store.read_org_flow_control.side_effect = None
    store.read_org_flow_control.return_value = payload()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert (await waiter).version == 1
    assert store.read_org_flow_control.await_count == 2
    assert reader.entries[42].users == 0


async def test_cache_evicts_inactive_lru_but_protects_active_pauses(env):
    reader, store, _, now = env
    reader.max_entries = 2
    pause = (now[0] + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    store.read_org_flow_control.side_effect = lambda org: payload(
        org_id=org, pause_until=pause if org != 43 else None)
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    await reader.check(43, None, 2)
    with pytest.raises(flow.PauseRejected):
        await reader.check(44, None, 2)
    assert set(reader.entries) == {42, 44}
    assert await reader.check(45, None, 2) is None
    assert store.read_org_flow_control.await_count == 3
    assert len(reader.entries) == 2
    now[0] += timedelta(hours=2)
    assert (await reader.check(45, None, 2)).version == 1
    assert set(reader.entries) == {44, 45}


async def test_cache_cannot_evict_a_read_owner_or_waiter(env):
    reader, store, _, _ = env
    reader.max_entries = 1
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(org):
        entered.set()
        await release.wait()
        return payload(org_id=org)
    store.read_org_flow_control.side_effect = read
    owner = asyncio.create_task(reader.check(42, None, 2))
    await entered.wait()
    waiter = asyncio.create_task(reader.check(42, None, 2))
    await asyncio.sleep(0)
    assert await reader.check(43, None, 2) is None
    assert reader.entries[42].users == 2
    release.set()
    snapshots = await asyncio.gather(owner, waiter)
    assert snapshots == [reader.entries[42].snapshot] * 2
    assert reader.entries[42].users == 0


@pytest.mark.parametrize("blocked_on_semaphore", [True, False])
async def test_read_and_semaphore_wait_have_a_total_deadline(env, monkeypatch, blocked_on_semaphore):
    reader, store, _, _ = env
    monkeypatch.setattr(flow, "_READ_TIMEOUT_S", PRODUCTION_READ_TIMEOUT_S)
    if blocked_on_semaphore:
        reader.reads = asyncio.Semaphore(0)
    else:
        async def blocked(_org):
            await asyncio.Event().wait()
        store.read_org_flow_control.side_effect = blocked
    assert await asyncio.wait_for(reader.check(42, None, 2), timeout=1) is None
    assert store.read_org_flow_control.await_count == (0 if blocked_on_semaphore else 1)


async def test_logs_report_pause_capability_and_no_payload_or_exception(env, monkeypatch):
    reader, store, clock, _ = env
    records = []
    monkeypatch.setattr(flow.log, "log", lambda level, fmt, *args: records.append(fmt % args))
    store.read_org_flow_control.return_value = payload(version=2)
    await reader.check(42, "observed-ref", 2)
    assert "org_flow_control_version_observed" in records[0]
    parsed = json.loads(records[0].split(" ", 1)[1])
    assert parsed["capability"] == "pause" and parsed["config_version"] == 2
    assert parsed["capabilities"] == ["pause", "rate", "concurrency"]
    assert parsed["max_in_flight"] is None
    assert parsed["rate_limit"] is None
    assert parsed["org_id"] == 42 and parsed["call_ref"] == "observed-ref"
    assert parsed["worker_pid"] > 0
    store.read_org_flow_control.side_effect = RuntimeError("private-token-or-url")
    clock[0] += 3
    assert await reader.check(42, "failure-ref", 2) is None
    assert "private-token-or-url" not in "\n".join(records)
    assert "redis_unavailable" in records[-1]


async def test_same_version_key_order_is_not_a_conflict(env):
    reader, store, clock, _ = env
    original = payload(rate_limit={"rps": 1, "burst": 2})
    store.read_org_flow_control.return_value = original
    assert (await reader.check(42, None, 2)).version == 1
    clock[0] += 3
    store.read_org_flow_control.return_value = json.dumps(
        json.loads(original), sort_keys=True, indent=2)
    assert (await reader.check(42, None, 2)).version == 1


async def test_logging_failure_cannot_replace_pause_refusal(env, monkeypatch):
    reader, store, _, now = env
    store.read_org_flow_control.return_value = payload(
        pause_until=(now[0] + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"))
    def broken_log(*_args):
        raise OSError("log sink unavailable")
    monkeypatch.setattr(flow.log, "log", broken_log)
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)


def test_valid_publisher_year_zero_is_already_expired():
    snapshot = flow._parse(payload(pause_until="0000-02-29T00:00:00Z"), 42)
    assert snapshot.pause_until == datetime.min.replace(tzinfo=timezone.utc)


async def test_pause_and_capacity_logs_are_bounded(env, caplog):
    reader, store, clock, _ = env
    caplog.set_level(logging.INFO, logger=flow.log.name)
    reader.max_entries = 1
    store.read_org_flow_control.return_value = payload(pause_until="2035-01-01T00:00:00Z")
    for _ in range(100):
        with pytest.raises(flow.PauseRejected):
            await reader.check(42, None, 2)
        assert await reader.check(43, None, 2) is None
    pauses = [r for r in caplog.records if r.getMessage().startswith("org_flow_control_paused ")]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(pauses) == len(warnings) == 1
    assert "cache_capacity" in warnings[0].getMessage()
    clock[0] += 5
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    assert json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])["suppressed_since_last_log"] == 99
    assert await reader.check(43, None, 2) is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


async def test_refresh_failure_diagnostics_are_rate_limited(env, caplog):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = flow.kv.FlowControlReadFailure.UNAVAILABLE
    for _ in range(25):
        assert await reader.check(42, None, 0.2) is None
        clock[0] += 0.2
    assert len(caplog.records) == 1
    clock[0] += 1
    assert await reader.check(42, None, 0.2) is None
    assert len(caplog.records) == 2
    assert json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])["suppressed_since_last_log"] == 24


async def test_real_publisher_contract_disables_pauses_and_resumes(env):
    reader, store, clock, now = env
    fixtures = Path(__file__).parent / "fixtures" / "org-flow-control"
    now[0] = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store.read_org_flow_control.return_value = (fixtures / "disabled.json").read_text()
    assert (await reader.check(42, None, 2)).version == 1
    clock[0] += 3
    store.read_org_flow_control.return_value = (fixtures / "paused.json").read_text()
    with pytest.raises(flow.PauseRejected) as rejected:
        await reader.check(42, None, 2)
    assert rejected.value.retry_after_s == 2
    assert rejected.value.detail["config_version"] == 2
    assert reader.entries[42].snapshot.pause_until.microsecond == 234000
    clock[0] += 3
    store.read_org_flow_control.return_value = (fixtures / "resumed.json").read_text()
    snapshot = await reader.check(42, None, 2)
    assert snapshot.version == 3 and snapshot.max_in_flight == 2
    assert snapshot.rate_limit == flow.RateLimit(rps=0.5, burst=5)


def test_parser_retains_published_concurrency_and_defaults_to_disabled():
    assert flow._parse(payload(), 42).max_in_flight is None
    snapshot = flow._parse(payload(version=4, concurrency_limit={"max_in_flight": 7}), 42)
    assert snapshot.version == 4
    assert snapshot.max_in_flight == 7


async def test_concurrency_updates_and_null_are_full_snapshot_replacements(env):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = payload(
        version=1, concurrency_limit={"max_in_flight": 7})
    first = await reader.check(42, None, 2)
    assert first.max_in_flight == 7
    assert await reader.check(42, None, 2) is first
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(
        version=2, concurrency_limit={"max_in_flight": 2})
    second = await reader.check(42, None, 2)
    assert second.version == 2 and second.max_in_flight == 2
    assert first.max_in_flight == 7
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=3)
    disabled = await reader.check(42, None, 2)
    assert disabled.version == 3 and disabled.max_in_flight is None


@pytest.mark.parametrize("bad", ["invalid", payload(version=1),
                               payload(version=2, concurrency_limit={"max_in_flight": 9})])
async def test_invalid_concurrency_or_version_conflict_keeps_last_valid_limit(env, bad):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = payload(
        version=2, concurrency_limit={"max_in_flight": 3})
    snapshot = await reader.check(42, None, 2)
    clock[0] += 3
    store.read_org_flow_control.return_value = bad
    assert await reader.check(42, None, 2) is snapshot
    assert snapshot.max_in_flight == 3


async def test_cache_protects_published_concurrency_until_null_replaces_it(env):
    reader, store, clock, _ = env
    reader.max_entries = 2
    disabled = set()
    store.read_org_flow_control.side_effect = lambda org: payload(
        org_id=org, version=2 if org in disabled else 1,
        concurrency_limit={"max_in_flight": 2} if org not in disabled and org != 43 else None)
    assert (await reader.check(42, None, 2)).max_in_flight == 2
    assert (await reader.check(43, None, 2)).max_in_flight is None
    assert (await reader.check(44, None, 2)).max_in_flight == 2
    assert set(reader.entries) == {42, 44}
    assert await reader.check(45, None, 2) is None
    assert store.read_org_flow_control.await_count == 3
    disabled.add(42)
    clock[0] += 3
    assert (await reader.check(42, None, 2)).max_in_flight is None
    assert (await reader.check(45, None, 2)).max_in_flight == 2
    assert set(reader.entries) == {44, 45}


@pytest.mark.parametrize("rps,burst", [(2, 4), (0.5, 3), (9007199254740991, 2147483647)])
def test_parser_retains_published_rate_without_mutable_configuration(rps, burst):
    assert flow._parse(payload(), 42).rate_limit is None
    snapshot = flow._parse(payload(version=4, rate_limit={"rps": rps, "burst": burst}), 42)
    assert snapshot.version == 4
    assert snapshot.rate_limit == flow.RateLimit(rps=rps, burst=burst)
    with pytest.raises(FrozenInstanceError):
        snapshot.rate_limit.rps = 3
    with pytest.raises(FrozenInstanceError):
        snapshot.rate_limit = None


async def test_rate_updates_and_null_replace_one_complete_snapshot(env):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = payload(
        version=1, rate_limit={"rps": 0.5, "burst": 2})
    first = await reader.check(42, None, 2)
    assert first.rate_limit == flow.RateLimit(rps=0.5, burst=2)
    assert first.max_in_flight is None
    assert await reader.check(42, None, 2) is first
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(
        version=2, rate_limit={"rps": 2, "burst": 5},
        concurrency_limit={"max_in_flight": 3})
    second = await reader.check(42, None, 2)
    assert second.version == 2 and second.max_in_flight == 3
    assert second.rate_limit == flow.RateLimit(rps=2, burst=5)
    assert first.rate_limit == flow.RateLimit(rps=0.5, burst=2)
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=3)
    disabled = await reader.check(42, None, 2)
    assert disabled.version == 3
    assert disabled.rate_limit is None and disabled.max_in_flight is None


@pytest.mark.parametrize("bad", [
    flow.kv.FlowControlReadFailure.INVALID, "invalid", payload(version=1),
    payload(version=3, rate_limit={"rps": "3", "burst": 4}),
    payload(version=2, rate_limit={"rps": 3, "burst": 4}),
])
async def test_invalid_rate_or_version_conflict_retains_same_valid_snapshot(env, bad):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = payload(
        version=2, rate_limit={"rps": 0.5, "burst": 2},
        concurrency_limit={"max_in_flight": 3})
    snapshot = await reader.check(42, None, 2)
    clock[0] += 3
    store.read_org_flow_control.return_value = bad
    assert await reader.check(42, None, 2) is snapshot
    assert snapshot.rate_limit == flow.RateLimit(rps=0.5, burst=2)
    assert snapshot.max_in_flight == 3


@pytest.mark.parametrize("bad", [None, flow.kv.FlowControlReadFailure.UNAVAILABLE])
async def test_missing_or_unavailable_rate_snapshot_allows_and_can_recover(env, bad):
    reader, store, clock, _ = env
    original = payload(version=2, rate_limit={"rps": 0.5, "burst": 2})
    store.read_org_flow_control.return_value = original
    snapshot = await reader.check(42, None, 2)
    clock[0] += 3
    store.read_org_flow_control.return_value = bad
    assert await reader.check(42, None, 2) is None
    assert reader.entries[42].snapshot is (None if bad is None else snapshot)
    assert await reader.check(42, None, 2) is None
    clock[0] += 3
    store.read_org_flow_control.return_value = original
    recovered = await reader.check(42, None, 2)
    assert recovered.version == 2
    assert recovered.rate_limit == flow.RateLimit(rps=0.5, burst=2)


async def test_cache_protects_rate_only_rules_after_pause_expiry_until_null(env):
    reader, store, clock, now = env
    reader.max_entries = 2
    disabled = set()
    store.read_org_flow_control.side_effect = lambda org: payload(
        org_id=org, version=2 if org in disabled else 1,
        pause_until="2029-01-01T00:00:00Z",
        rate_limit={"rps": 0.5, "burst": 2} if org not in disabled and org != 43 else None)
    assert (await reader.check(42, None, 2)).rate_limit is not None
    assert (await reader.check(43, None, 2)).rate_limit is None
    assert (await reader.check(44, None, 2)).rate_limit is not None
    assert set(reader.entries) == {42, 44}
    now[0] += timedelta(days=10)
    assert await reader.check(45, None, 2) is None
    assert store.read_org_flow_control.await_count == 3
    disabled.add(42)
    clock[0] += 3
    assert (await reader.check(42, None, 2)).rate_limit is None
    assert (await reader.check(45, None, 2)).rate_limit is not None
    assert set(reader.entries) == {44, 45}


async def test_version_observations_report_rate_and_clear_it_for_missing_key(env, monkeypatch):
    reader, store, clock, _ = env
    events = []
    monkeypatch.setattr(flow.log, "log", lambda level, fmt, *args: events.append(
        json.loads((fmt % args).split(" ", 1)[1])))
    store.read_org_flow_control.return_value = payload(
        version=2, rate_limit={"rps": 0.5, "burst": 2},
        concurrency_limit={"max_in_flight": 3})
    await reader.check(42, "populated", 2)
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=3)
    await reader.check(42, "disabled", 2)
    clock[0] += 3
    store.read_org_flow_control.return_value = None
    assert await reader.check(42, "missing", 2) is None
    assert [event["config_version"] for event in events] == [2, 3, 0]
    assert [event["rate_limit"] for event in events] == [{"rps": 0.5, "burst": 2}, None, None]
    assert [event["max_in_flight"] for event in events] == [3, None, None]
    assert all(event["capabilities"] == ["pause", "rate", "concurrency"] for event in events)
