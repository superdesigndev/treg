"""Quota transport failures back off without serializing healthy admission."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from treg.infra import org_endpoint_quota as quota


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(quota, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def client_store(monkeypatch):
    client = SimpleNamespace(eval=AsyncMock(return_value=[0]), aclose=AsyncMock())
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: client)
    return client, quota.RedisQuotaStore("redis://synthetic.invalid")


@pytest.mark.parametrize("failure", [
    RedisConnectionError("synthetic connection refused"),
    RedisTimeoutError("synthetic timeout"),
    TimeoutError("synthetic uncertain write"),
])
async def test_failure_skips_redis_for_five_seconds(client_store, clock, failure):
    client, store = client_store
    client.eval.side_effect = [failure, [0], [0]]
    assert await store.admit(42, "example.read") == "unavailable"
    clock[0] += 4.999
    assert await store.admit(43, "example.other") == "unavailable"
    assert client.eval.await_count == 1
    clock[0] += .001
    assert await store.admit(42, "example.read") == "allowed"
    assert await store.admit(42, "example.read") == "allowed"
    assert client.eval.await_count == 3


async def test_blackhole_times_out_once_then_skips_redis(client_store, clock, monkeypatch):
    client, store = client_store
    monkeypatch.setattr(quota, "_TIMEOUT_S", .01)

    async def blackhole(*args):
        await asyncio.Event().wait()

    client.eval.side_effect = blackhole
    assert await store.admit(42, "example.read") == "unavailable"
    assert await store.admit(43, "example.other") == "unavailable"
    assert client.eval.await_count == 1


@pytest.mark.parametrize("result,outcome", [
    ([0], "allowed"), ([1, 1], "exceeded"), ([2, 1], "unavailable"),
])
async def test_one_recovery_probe_while_other_calls_skip_redis(client_store, clock, result, outcome):
    client, store = client_store
    client.eval.side_effect = RedisConnectionError("synthetic offline")
    assert await store.admit(42, "example.read") == "unavailable"
    clock[0] += 5
    entered, release = asyncio.Event(), asyncio.Event()

    async def recovering(*args):
        entered.set()
        await release.wait()
        return result

    client.eval.side_effect = recovering
    probe = asyncio.create_task(store.admit(42, "example.read"))
    try:
        await entered.wait()
        async with asyncio.timeout(.2):
            bypasses = await asyncio.gather(*[
                store.admit(43, "example.other") for _ in range(20)
            ])
        assert bypasses == ["unavailable"] * 20
        assert client.eval.await_count == 2
    finally:
        release.set()
        assert await probe == outcome
    client.eval.side_effect = None
    assert await store.admit(42, "example.read") == "allowed"
    assert client.eval.await_count == 3


async def test_failed_probe_starts_another_cooldown(client_store, clock):
    client, store = client_store
    client.eval.side_effect = [RedisConnectionError(), RedisConnectionError(), [0]]
    assert await store.admit(42, "example.read") == "unavailable"
    clock[0] += 5
    assert await store.admit(42, "example.read") == "unavailable"
    clock[0] += 4.999
    assert await store.admit(42, "example.read") == "unavailable"
    assert client.eval.await_count == 2
    clock[0] += .001
    assert await store.admit(42, "example.read") == "allowed"
    assert client.eval.await_count == 3


@pytest.mark.parametrize("recovering", [False, True])
async def test_cancellation_does_not_open_or_lock_the_breaker(client_store, clock, recovering):
    client, store = client_store
    if recovering:
        client.eval.side_effect = RedisConnectionError()
        assert await store.admit(42, "example.read") == "unavailable"
        clock[0] += 5
    entered = asyncio.Event()

    async def waiting(*args):
        entered.set()
        await asyncio.Event().wait()

    client.eval.side_effect = waiting
    pending = asyncio.create_task(store.admit(42, "example.read"))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    client.eval.side_effect = None
    assert await store.admit(42, "example.read") == "allowed"
    assert client.eval.await_count == (3 if recovering else 2)


async def test_unknown_rule_does_not_bypass_another_exhausted_rule(client_store, clock):
    client, store = client_store
    client.eval.side_effect = [[2, 1], [1, 1]]
    assert await store.admit(42, "example.corrupt") == "unavailable"
    assert await store.admit(43, "example.exhausted") == "exceeded"
    assert client.eval.await_count == 2


async def test_another_store_checks_its_rules_during_local_cooldown(client_store, clock):
    client, store = client_store
    other = quota.RedisQuotaStore("redis://synthetic.invalid")
    client.eval.side_effect = [RedisConnectionError(), [1, 1]]
    assert await store.admit(42, "example.read") == "unavailable"
    assert await other.admit(42, "example.read") == "exceeded"
    assert await store.admit(42, "example.read") == "unavailable"
    assert client.eval.await_count == 2


async def test_healthy_calls_are_not_serialized(client_store, clock):
    client, store = client_store
    entered, release = asyncio.Event(), asyncio.Event()
    concurrent = 0

    async def healthy(*args):
        nonlocal concurrent
        concurrent += 1
        if concurrent == 2:
            entered.set()
        await release.wait()
        return [0]

    client.eval.side_effect = healthy
    calls = [asyncio.create_task(store.admit(42, "example.read")) for _ in range(2)]
    try:
        async with asyncio.timeout(.2):
            await entered.wait()
    finally:
        release.set()
        assert await asyncio.gather(*calls) == ["allowed", "allowed"]
    assert client.eval.await_count == 2


async def test_late_healthy_reply_does_not_close_a_newly_opened_breaker(client_store, clock):
    client, store = client_store
    entered, release = asyncio.Event(), asyncio.Event()

    async def mixed(*args):
        if args[-1] == "rule:42:example.slow":
            entered.set()
            await release.wait()
            return [0]
        raise RedisConnectionError("synthetic failure")

    client.eval.side_effect = mixed
    earlier = asyncio.create_task(store.admit(42, "example.slow"))
    await entered.wait()
    assert await store.admit(42, "example.read") == "unavailable"
    release.set()
    assert await earlier == "allowed"
    assert await store.admit(42, "example.read") == "unavailable"
    assert client.eval.await_count == 2
