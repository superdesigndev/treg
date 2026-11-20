"""Bounded published-rule reads against disposable loopback Redis."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from pydantic import ValidationError

from treg.config import Settings
from treg.infra import kv, org_concurrency as guard, org_flow_control as flow


def test_flow_settings_are_default_off_and_require_explicit_redis():
    assert not Settings(_env_file=None).org_flow_control_enabled
    for value in ("", "http://localhost", "redis://[invalid"):
        with pytest.raises(ValidationError, match="valid Redis kv_url"):
            Settings(_env_file=None, org_flow_control_enabled=True, kv_url=value)
    assert Settings(_env_file=None, org_flow_control_enabled=True,
                    kv_url="redis://localhost").org_flow_control_refresh_s == 2


@pytest.mark.parametrize("value", [0, 0.1, 11, float("inf"), float("nan")])
def test_flow_refresh_interval_is_bounded(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, org_flow_control_refresh_s=value)


async def _wait_for_redis(store, *, timeout_s=5):
    # CI scheduling can exhaust a single PING's 200 ms budget. Retry only readiness;
    # all operations under test retain their production deadlines and retry policy.
    try:
        async with asyncio.timeout(timeout_s):
            while not await store.ping():
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise AssertionError("disposable Redis did not become ready within the setup deadline") from exc


async def test_redis_readiness_recovers_after_a_ping_timeout(monkeypatch):
    calls = 0

    async def ping():
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()  # RedisStore.ping turns its deadline into False.
        return True

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(ping=ping)
    monkeypatch.setattr(kv, "_note_fault", lambda _exc: None)
    await _wait_for_redis(store)
    assert calls == 2


@pytest.mark.parametrize("blocked", [False, True])
async def test_redis_readiness_fails_within_its_overall_deadline(blocked):
    async def ping():
        if blocked:
            await asyncio.Event().wait()
        return False

    with pytest.raises(AssertionError, match="Redis did not become ready"):
        await asyncio.wait_for(_wait_for_redis(SimpleNamespace(ping=ping), timeout_s=0.02), 1)


async def test_redis_readiness_propagates_cancellation():
    entered = asyncio.Event()

    async def ping():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_wait_for_redis(SimpleNamespace(ping=ping)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.fixture
async def redis_store():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    store = kv.RedisStore(url)
    org_id = uuid4().int % 2147483647 + 1
    key = f"treg:traffic-control:org:{org_id}"
    try:
        await _wait_for_redis(store)
        try:
            yield store, org_id, key
        finally:
            await store._client.delete(key, f"treg:org-concurrency:v1:{org_id}")
    finally:
        await store.aclose()


async def test_read_distinguishes_missing_empty_oversize_and_wrong_type(redis_store):
    store, org_id, key = redis_store
    assert await store.read_org_flow_control(org_id) is None
    await store._client.set(key, "")
    assert await store.read_org_flow_control(org_id) == ""
    await store._client.set(key, "x" * 32769)
    assert await store.read_org_flow_control(org_id) is kv.FlowControlReadFailure.INVALID
    await store._client.delete(key)
    await store._client.lpush(key, "synthetic wrong type")
    assert await store.read_org_flow_control(org_id) is kv.FlowControlReadFailure.INVALID
    await store._client.delete(key)
    await store._client.set(key, b"\xff")
    assert await store.read_org_flow_control(org_id) is kv.FlowControlReadFailure.INVALID


async def test_published_json_is_read_without_mutation_and_new_versions_resume(redis_store, monkeypatch):
    store, org_id, key = redis_store
    now = datetime.now(timezone.utc)
    config = {"schema_version": 1,
              "pause_until": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
              "rate_limit": None, "concurrency_limit": None}
    envelope = {"org_id": org_id, "version": 1, "config": config}
    raw = json.dumps(envelope)
    await store._client.set(key, raw)
    assert await store.read_org_flow_control(org_id) == raw
    assert await store._client.ttl(key) == -1
    monkeypatch.setattr(kv, "store", lambda: store)
    reader = flow.Reader()
    with pytest.raises(flow.PauseRejected) as failure:
        await reader.check(org_id, "synthetic-call-ref", 0)
    assert failure.value.detail["config_version"] == 1
    envelope["version"] = 2
    envelope["config"]["pause_until"] = None
    await store._client.set(key, json.dumps(envelope))
    assert (await reader.check(org_id, "synthetic-resume-ref", 0)).version == 2
    assert await store._client.ttl(key) == -1


async def test_unavailable_and_local_backend_never_look_like_missing():
    store = kv.RedisStore("redis://127.0.0.1:1")
    try:
        assert await store.read_org_flow_control(42) is kv.FlowControlReadFailure.UNAVAILABLE
        assert await kv.LocalStore().read_org_flow_control(42) is kv.FlowControlReadFailure.UNAVAILABLE
    finally:
        await store.aclose()


async def test_read_timeout_and_error_do_not_expose_data_or_retry(monkeypatch):
    class Slow:
        async def eval(self, *_args):
            await asyncio.sleep(10)

    store = kv.RedisStore("redis://127.0.0.1:1")
    client = store._client
    try:
        monkeypatch.setattr(store, "_client", Slow())
        assert await asyncio.wait_for(store.read_org_flow_control(42), 1) is kv.FlowControlReadFailure.UNAVAILABLE
    finally:
        await client.aclose()


async def test_read_cancellation_propagates(monkeypatch):
    entered = asyncio.Event()

    class Blocked:
        async def eval(self, *_args):
            entered.set()
            await asyncio.Event().wait()

    store = kv.RedisStore("redis://127.0.0.1:1")
    client = store._client
    try:
        monkeypatch.setattr(store, "_client", Blocked())
        task = asyncio.create_task(store.read_org_flow_control(42))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.aclose()


@pytest.mark.parametrize("bad", ["wrong_type", "oversize", "utf8", "json"])
async def test_malformed_redis_data_keeps_pause_until_original_expiry(redis_store, monkeypatch, bad):
    store, org_id, key = redis_store
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(flow, "_utcnow", lambda: now[0])
    monkeypatch.setattr(kv, "store", lambda: store)
    config = {"schema_version": 1, "pause_until": "2030-01-01T00:00:01Z",
              "rate_limit": None, "concurrency_limit": None}
    await store._client.set(key, json.dumps({"org_id": org_id, "version": 1, "config": config}))
    reader = flow.Reader()
    with pytest.raises(flow.PauseRejected):
        await reader.check(org_id, None, 0)
    if bad == "wrong_type":
        await store._client.delete(key)
        await store._client.lpush(key, "synthetic")
    else:
        await store._client.set(key, {"oversize": "x" * 32769, "utf8": b"\xff", "json": "{"}[bad])
    with pytest.raises(flow.PauseRejected):
        await reader.check(org_id, None, 0)
    now[0] += timedelta(seconds=2)
    assert (await reader.check(org_id, None, 0)).version == 1


async def test_published_concurrency_is_shared_and_updates_preserve_active_owners(
    redis_store, monkeypatch,
):
    first, org_id, key = redis_store
    second = kv.RedisStore(os.environ["TREG_TEST_KV_URL"])
    readers = flow.Reader(), flow.Reader()
    slots = []
    slot_key = f"treg:org-concurrency:v1:{org_id}"

    async def publish(version, limit):
        config = {"schema_version": 1, "pause_until": None, "rate_limit": None,
                  "concurrency_limit": {"max_in_flight": limit} if limit else None}
        await first._client.set(key, json.dumps({
            "org_id": org_id, "version": version, "config": config}))

    async def admit(worker):
        store = (first, second)[worker]
        monkeypatch.setattr(kv, "store", lambda: store)
        snapshot = await readers[worker].check(org_id, None, 0)
        slot = await guard.acquire(
            org_id, limit=snapshot.max_in_flight if snapshot else None,
            version=snapshot.version if snapshot else 0)
        if slot is not None:
            slots.append(slot)
        return slot

    try:
        await publish(1, 2)
        initial = [await admit(0), await admit(1)]
        assert all(initial)
        with pytest.raises(guard.AdmissionRejected) as rejected:
            await admit(1)
        assert rejected.value.status_code == 429
        assert rejected.value.detail["config_version"] == 1
        assert await first._client.zcard(slot_key) == 2

        # Lowering the cap keeps both accepted calls and their owner tokens intact.
        await publish(2, 1)
        with pytest.raises(guard.AdmissionRejected) as rejected:
            await admit(0)
        assert rejected.value.detail["config_version"] == 2
        assert await first._client.zcard(slot_key) == 2

        # Neither a null control nor deletion resets already active occupancy.
        await publish(3, None)
        assert await admit(1) is None
        assert await first._client.zcard(slot_key) == 2
        await first._client.delete(key)
        assert await admit(0) is None
        assert await first._client.zcard(slot_key) == 2

        await publish(4, 1)
        await initial[0].finish()
        with pytest.raises(guard.AdmissionRejected) as rejected:
            await admit(1)
        assert rejected.value.detail["config_version"] == 4
        await initial[1].finish()
        replacement = await admit(0)
        assert replacement is not None
        assert replacement.token not in {slot.token for slot in initial}
        assert await first._client.zcard(slot_key) == 1
    finally:
        await asyncio.gather(*(slot.finish() for slot in slots))
        await second.aclose()


async def test_bad_publication_retains_concurrency_but_read_outage_allows(
    redis_store, monkeypatch,
):
    store, org_id, key = redis_store
    config = {"schema_version": 1, "pause_until": None, "rate_limit": None,
              "concurrency_limit": {"max_in_flight": 1}}
    await store._client.set(key, json.dumps({
        "org_id": org_id, "version": 2, "config": config}))
    monkeypatch.setattr(kv, "store", lambda: store)
    reader = flow.Reader()
    snapshot = await reader.check(org_id, None, 0)
    slot = await guard.acquire(org_id, limit=snapshot.max_in_flight, version=snapshot.version)
    try:
        for raw in ("{", json.dumps({"org_id": org_id, "version": 1, "config": config})):
            await store._client.set(key, raw)
            assert await reader.check(org_id, None, 0) is snapshot
            with pytest.raises(guard.AdmissionRejected) as rejected:
                await guard.acquire(org_id, limit=snapshot.max_in_flight, version=snapshot.version)
            assert rejected.value.detail["config_version"] == 2

        monkeypatch.setattr(kv, "store", lambda: kv.LocalStore())
        assert await reader.check(org_id, None, 0) is None
        assert await guard.acquire(org_id, limit=None, version=0) is None
        assert await store._client.zcard(slot.key) == 1
    finally:
        await slot.finish()
