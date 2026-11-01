"""Quota admission against an isolated Redis subprocess, never a configured server."""
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from treg.config import Settings
from treg.infra import org_endpoint_quota as quota


@pytest.fixture
async def store():
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("requires local redis-server")
    directory = tempfile.TemporaryDirectory(prefix="quota-", dir="/tmp")
    socket = Path(directory.name) / "redis.sock"
    process = subprocess.Popen(
        [executable, "--port", "0", "--unixsocket", str(socket),
         "--save", "", "--appendonly", "no", "--dir", directory.name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = quota.RedisQuotaStore(f"unix://{socket}")
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    await client._client.ping()
                    break
                except RedisConnectionError:
                    await asyncio.sleep(0.01)
        await client._client.hset(quota.KEY, "__schema", "1")
        yield client
    finally:
        await client.aclose()
        process.terminate()
        process.wait(timeout=5)
        directory.cleanup()


async def put_rule(store, *, org=42, endpoint="example.read", **overrides):
    state = dict(version=1, limit=3, used=0, enabled=True) | overrides
    await store._client.hset(quota.KEY, f"rule:{org}:{endpoint}", json.dumps(state))


async def state(store, org=42, endpoint="example.read"):
    return json.loads(await store._client.hget(quota.KEY, f"rule:{org}:{endpoint}"))


def test_settings_default_off_and_require_explicit_quota_store():
    assert Settings(_env_file=None).org_endpoint_quota_enabled is False
    for url in ("", "http://localhost", "redis://[invalid"):
        with pytest.raises(ValidationError, match="valid Redis quota URL"):
            Settings(_env_file=None, org_endpoint_quota_enabled=True,
                     org_endpoint_quota_redis_url=url)
    assert Settings(_env_file=None, org_endpoint_quota_enabled=True,
                    org_endpoint_quota_redis_url="redis://localhost").org_endpoint_quota_enabled


async def test_concurrent_instances_never_overissue(store):
    other = quota.RedisQuotaStore(
        "unix://" + store._client.connection_pool.connection_kwargs["path"])
    await put_rule(store, limit=17)
    results = await asyncio.gather(*[
        (store if i % 2 else other).admit(42, "example.read") for i in range(80)
    ])
    assert results.count("allowed") == 17
    assert results.count("exceeded") == 63
    assert (await state(store))["used"] == 17
    assert await store._client.pttl(quota.KEY) == -1
    await other.aclose()


async def test_zero_quota_and_exact_pairs_and_disabled_tombstone(store):
    await put_rule(store, limit=0, used=5)
    assert await store.admit(42, "example.read") == "exceeded"
    assert await store.admit(43, "example.read") == "allowed"
    assert await store.admit(42, "example.other") == "allowed"
    assert (await state(store))["used"] == 5
    await put_rule(store, limit=0, used=5, enabled=False)
    assert await store.admit(42, "example.read") == "allowed"
    assert (await state(store))["used"] == 5


async def test_healthy_parent_and_child_admission_is_all_or_nothing(store):
    parent = (42, "example.route")
    child = (42, "example.read")
    await put_rule(store, endpoint=parent[1], limit=5)
    await put_rule(store, limit=0)
    assert await store.admit_many((parent, child)) == ("exceeded", child)
    assert (await state(store, endpoint=parent[1]))["used"] == 0
    assert (await state(store))["used"] == 0
    await put_rule(store, endpoint=parent[1], limit=0)
    await put_rule(store, limit=5)
    assert await store.admit_many((parent, child)) == ("exceeded", parent)
    assert (await state(store))["used"] == 0
    await put_rule(store, endpoint=parent[1], limit=5)
    assert await store.admit_many((parent, child, child)) == ("allowed", None)
    assert (await state(store, endpoint=parent[1]))["used"] == 1
    assert (await state(store))["used"] == 1
    await store._client.hset(quota.KEY, "rule:42:example.read", '{"used":null}')
    assert await store.admit_many((parent, child)) == ("unavailable", child)
    assert (await state(store, endpoint=parent[1]))["used"] == 2
    assert await store._client.hget(quota.KEY, "rule:42:example.read") == '{"used":null}'


@pytest.mark.parametrize("unknown_first", [True, False])
async def test_unknown_rule_never_masks_another_known_exhausted_target(store, unknown_first):
    parent, child = (42, "example.route"), (42, "example.read")
    await store._client.hset(quota.KEY, "rule:42:example.route", '{"used":null}')
    await put_rule(store, limit=0)
    targets = (parent, child) if unknown_first else (child, parent)
    assert await store.admit_many(targets) == ("exceeded", child)
    assert (await state(store))["used"] == 0
    assert await store._client.hget(quota.KEY, "rule:42:example.route") == '{"used":null}'


async def test_concurrent_unknown_parent_preserves_known_child_allowance(store):
    other = quota.RedisQuotaStore(
        "unix://" + store._client.connection_pool.connection_kwargs["path"])
    parent, child = (42, "example.route"), (42, "example.read")
    await store._client.hset(quota.KEY, "rule:42:example.route", '{"used":null}')
    await put_rule(store, limit=17)
    try:
        results = await asyncio.gather(*[
            (store if i % 2 else other).admit_many((parent, child)) for i in range(80)
        ])
        assert results.count(("unavailable", parent)) == 17
        assert results.count(("exceeded", child)) == 63
        assert (await state(store))["used"] == 17
        assert await store._client.hget(quota.KEY, "rule:42:example.route") == '{"used":null}'
    finally:
        await other.aclose()


async def test_concurrent_parent_child_batches_never_partially_consume(store):
    other = quota.RedisQuotaStore(
        "unix://" + store._client.connection_pool.connection_kwargs["path"])
    targets = ((42, "example.route"), (42, "example.read"))
    await put_rule(store, endpoint="example.route", limit=17)
    await put_rule(store, limit=40)
    try:
        results = await asyncio.gather(*[
            (store if i % 2 else other).admit_many(targets) for i in range(80)
        ])
        assert results.count(("allowed", None)) == 17
        assert results.count(("exceeded", targets[0])) == 63
        assert (await state(store, endpoint="example.route"))["used"] == 17
        assert (await state(store))["used"] == 17
    finally:
        await other.aclose()


async def test_missing_namespace_schema_and_expiry_never_mean_no_rules(store):
    await store._client.delete(quota.KEY)
    assert await store.admit(42, "example.read") == "unavailable"
    await put_rule(store)
    assert await store.admit(42, "example.read") == "unavailable"
    await store._client.hset(quota.KEY, "__schema", "2")
    assert await store.admit(42, "example.read") == "unavailable"
    await store._client.hset(quota.KEY, "__schema", "1")
    await store._client.expire(quota.KEY, 60)
    assert await store.admit(42, "example.read") == "unavailable"
    assert (await state(store))["used"] == 0


@pytest.mark.parametrize("raw", [
    "", "null", "[]", "{}", "not-json", '"a"', "x" * 1025,
    '{"version":1,"limit":3,"enabled":true}',
    '{"version":1,"limit":3,"used":null,"enabled":true}',
    '{"version":1,"limit":3,"used":-1,"enabled":true}',
    '{"version":1,"limit":3,"used":0.5,"enabled":true}',
    '{"version":1,"limit":3,"used":true,"enabled":true}',
    '{"version":1,"limit":3,"used":0,"enabled":"true"}',
    '{"version":1,"limit":2147483648,"used":0,"enabled":true}',
    '{"version":1,"limit":3,"used":0,"enabled":true,"extra":1}',
])
async def test_corrupt_or_missing_counter_is_never_reinitialized(store, raw):
    field = "rule:42:example.read"
    await store._client.hset(quota.KEY, field, raw)
    assert await store.admit(42, "example.read") == "unavailable"
    assert await store._client.hget(quota.KEY, field) == raw


@pytest.mark.parametrize("org,endpoint", [(0, "a.b"), (True, "a.b"),
                                         (42, "*"), (42, "a.*"), (42, "a:b")])
async def test_invalid_targets_do_not_touch_redis(org, endpoint):
    store = quota.RedisQuotaStore("redis://synthetic.invalid")
    store._client = None
    assert await store.admit(org, endpoint) == "unavailable"


async def test_unknown_transport_outcome_is_reported_without_retry():
    calls = 0

    async def uncertain(*args):
        nonlocal calls
        calls += 1
        raise TimeoutError("possibly applied")

    store = quota.RedisQuotaStore("redis://synthetic.invalid")
    store._client = SimpleNamespace(eval=uncertain)
    assert await store.admit(42, "example.read") == "unavailable"
    assert calls == 1


async def test_cancellation_propagates():
    async def cancelled(*args):
        raise asyncio.CancelledError

    store = quota.RedisQuotaStore("redis://synthetic.invalid")
    store._client = SimpleNamespace(eval=cancelled)
    with pytest.raises(asyncio.CancelledError):
        await store.admit(42, "example.read")


async def test_enabled_client_initialization_failure_is_reported(monkeypatch):
    def broken(_url):
        raise RuntimeError("client unavailable")

    monkeypatch.setattr(quota, "get_settings", lambda: SimpleNamespace(
        org_endpoint_quota_enabled=True, org_endpoint_quota_redis_url="redis://localhost"))
    monkeypatch.setattr(quota, "_store", None)
    monkeypatch.setattr(quota, "RedisQuotaStore", broken)
    assert await quota.admit(42, "example.read") == "unavailable"


async def test_disabled_feature_does_not_initialize_store(monkeypatch):
    monkeypatch.setattr(quota, "get_settings", lambda: SimpleNamespace(
        org_endpoint_quota_enabled=False))
    monkeypatch.setattr(quota, "_store", None)
    assert await quota.admit(42, "example.read") == "allowed"
    assert quota._store is None
