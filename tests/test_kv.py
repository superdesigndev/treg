"""The shared key-value store: windowed counters that fail closed."""
import os
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from treg.config import get_settings
from treg.infra import kv


async def test_redis_socket_budgets_allow_one_second():
    store = kv.RedisStore('redis://127.0.0.1:1')
    try:
        connection = store._client.connection_pool.make_connection()
        assert connection.socket_timeout == connection.socket_connect_timeout == 1.0
    finally:
        await store.aclose()


@pytest.mark.parametrize('operation', ('take', 'ping'))
@pytest.mark.parametrize('cancel', (False, True))
async def test_optional_operations_keep_short_deadlines_and_propagate_cancellation(
    monkeypatch, operation, cancel,
):
    entered, stopped = asyncio.Event(), asyncio.Event()
    faults = []
    monkeypatch.setattr(kv, '_note_fault', faults.append)

    async def blocked():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    pipe = MagicMock()
    pipe.__aenter__.return_value = pipe
    pipe.execute = AsyncMock(side_effect=blocked)
    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(pipeline=lambda **kwargs: pipe, ping=blocked)
    started = time.monotonic()
    task = asyncio.create_task(store.take('key', 5, 60) if operation == 'take' else store.ping())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not faults
        else:
            assert await task is False
            assert 0.15 <= time.monotonic() - started < 0.6
            assert len(faults) == 1 and isinstance(faults[0], TimeoutError)
        assert stopped.is_set()
        if operation == 'take':
            pipe.__aexit__.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_local_window_counts_then_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    store = kv.LocalStore()
    assert [await store.take('k', 2, 60) for _ in range(3)] == [True, True, False]
    assert await store.take('other', 2, 60)  # keys are independent
    clock[0] += 59
    assert not await store.take('k', 2, 60)
    clock[0] += 2  # the window started at the first take and is now over
    assert await store.take('k', 2, 60)


async def test_local_store_is_bounded(monkeypatch):
    monkeypatch.setattr(kv, '_LOCAL_MAX_KEYS', 4)
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    store = kv.LocalStore()
    for i in range(4):
        await store.take(f'old{i}', 1, 10)
    clock[0] += 11  # all expired: eviction drops them instead of growing
    await store.take('new', 1, 10)
    assert set(store._windows) == {'new'}
    for i in range(4):
        await store.take(f'live{i}', 1, 100)
    assert len(store._windows) <= 4  # live keys past the bound: the oldest windows go


async def test_unreachable_redis_fails_closed_and_fast():
    store = kv.RedisStore('redis://127.0.0.1:1')
    started = time.monotonic()
    assert await store.take('k', 5, 60) is False
    assert await store.ping() is False
    assert await store.acquire_lease('lease', 'token', 1000) == 'unavailable'
    assert await store.renew_lease('lease', 'token', 1000) == 'unavailable'
    assert await store.release_lease('lease', 'token') == 'unavailable'
    assert time.monotonic() - started < 2
    await store.aclose()


def test_store_follows_configuration(monkeypatch):
    monkeypatch.setattr(kv, '_store', None)
    monkeypatch.setattr(get_settings(), 'kv_url', '')
    assert isinstance(kv.store(), kv.LocalStore) and not kv.configured()
    monkeypatch.setattr(kv, '_store', None)
    monkeypatch.setattr(get_settings(), 'kv_url', 'redis://127.0.0.1:1')
    assert isinstance(kv.store(), kv.RedisStore) and kv.configured()
    monkeypatch.setattr(kv, '_store', None)


async def test_admin_kv_reports_the_fallback(clients, monkeypatch):
    assert (await clients.get('/admin/kv')).status_code in (401, 403)
    monkeypatch.setattr(get_settings(), 'admin_token', 'kv-admin')
    response = await clients.get('/admin/kv', headers={'X-Treg-Token': 'kv-admin'})
    assert response.json() == {'configured': False, 'reachable': True}


@pytest.mark.skipif(not os.environ.get('TREG_TEST_KV_URL'), reason='set TREG_TEST_KV_URL to a scratch Redis')
async def test_real_redis_window():
    store = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    key = f'treg-test:{time.time_ns()}'
    try:
        assert await store.ping()
        assert [await store.take(key, 2, 5) for _ in range(3)] == [True, True, False]
        ttl = await store._client.ttl(key)
        assert 0 < ttl <= 5
    finally:
        await store._client.delete(key)
        await store.aclose()


async def test_local_counters_do_not_claim_distributed_lease_support():
    store = kv.LocalStore()
    assert await store.acquire_lease('lease', 'owner', 1000) == 'unavailable'
    assert await store.renew_lease('lease', 'owner', 1000) == 'unavailable'
    assert await store.release_lease('lease', 'owner') == 'unavailable'


@pytest.mark.skipif(not os.environ.get('TREG_TEST_KV_URL'), reason='set TREG_TEST_KV_URL to a scratch Redis')
async def test_real_redis_clients_exclude_each_other_and_only_the_owner_releases():
    first = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    second = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    key = f'treg-test:money-lease:{time.time_ns()}'
    try:
        results = await asyncio.gather(first.acquire_lease(key, 'first', 3000),
                                       second.acquire_lease(key, 'second', 3000))
        assert sorted(results) == ['acquired', 'busy']
        winner = 'first' if results[0] == 'acquired' else 'second'
        loser = 'second' if winner == 'first' else 'first'
        assert await second.release_lease(key, loser) == 'lost'
        assert await first.renew_lease(key, winner, 5000) == 'renewed'
        assert await first._client.pttl(key) > 3000
        assert await second.release_lease(key, winner) == 'released'
        assert await first._client.get(key) is None
    finally:
        await first._client.delete(key)
        await asyncio.gather(first.aclose(), second.aclose())


@pytest.mark.skipif(not os.environ.get('TREG_TEST_KV_URL'), reason='set TREG_TEST_KV_URL to a scratch Redis')
async def test_real_redis_expired_owner_cannot_renew_or_delete_replacement():
    first = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    second = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    key = f'treg-test:money-lease:{time.time_ns()}'
    try:
        assert await first.acquire_lease(key, 'old-owner', 20) == 'acquired'
        await asyncio.sleep(0.05)
        assert await second.acquire_lease(key, 'new-owner', 3000) == 'acquired'
        assert await first.renew_lease(key, 'old-owner', 5000) == 'lost'
        assert await first.release_lease(key, 'old-owner') == 'lost'
        assert await second._client.get(key) == 'new-owner'
    finally:
        await first._client.delete(key)
        await asyncio.gather(first.aclose(), second.aclose())
