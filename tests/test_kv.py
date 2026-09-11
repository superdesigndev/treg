"""The shared key-value store: windowed counters that fail closed."""
import os
import time

import pytest

from treg.config import get_settings
from treg.infra import kv


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
