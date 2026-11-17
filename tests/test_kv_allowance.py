"""A team's allowance counter in the shared store: one figure every process spends from."""
import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from treg.infra import kv

real_redis = pytest.mark.skipif(
    not os.environ.get('TREG_TEST_KV_URL'), reason='set TREG_TEST_KV_URL to a scratch Redis')


@pytest.fixture
async def counter():
    store = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    org_id = time.time_ns() % 2_000_000_000
    try:
        yield store, org_id
    finally:
        await store._client.delete(kv._allowance_key(org_id))
        await store.aclose()


@real_redis
async def test_takes_stop_at_what_the_counter_holds(counter):
    store, org_id = counter
    assert await store.allowance_take(org_id, 1) == 0
    assert await store.allowance_add(org_id, 7, 250, 60_000) == 250
    assert [await store.allowance_take(org_id, 100) for _ in range(3)] == [7, 7, 0]
    assert await store.allowance_take(org_id, 50) == 7
    assert await store.allowance_take(org_id, 1) == 0
    assert 0 < await store._client.pttl(kv._allowance_key(org_id)) <= 60_000


@real_redis
async def test_concurrent_takes_from_two_clients_never_overspend(counter):
    store, org_id = counter
    other = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    try:
        await store.allowance_add(org_id, 3, 1_000, 60_000)
        taken = await asyncio.gather(*(
            client.allowance_take(org_id, 30) for _ in range(40) for client in (store, other)))
    finally:
        await other.aclose()
    assert sum(1 for epoch in taken if epoch == 3) == 33 and set(taken) == {0, 3}
    assert await store.allowance_drop(org_id) == 10


@real_redis
async def test_a_give_back_reaches_only_the_counter_it_was_taken_from(counter):
    store, org_id = counter
    await store.allowance_add(org_id, 7, 100, 60_000)
    assert await store.allowance_take(org_id, 100) == 7
    assert await store.allowance_give_back(org_id, 7, 30) is True
    assert await store.allowance_give_back(org_id, 8, 30) is False
    assert await store.allowance_add(org_id, 7, 20, 60_000) == 50
    assert await store.allowance_add(org_id, 9, 500, 60_000) == 500
    assert await store.allowance_give_back(org_id, 7, 30) is False
    assert await store.allowance_take(org_id, 500) == 9


@real_redis
async def test_dropping_returns_what_was_left_and_ends_takes_and_give_backs(counter):
    store, org_id = counter
    assert await store.allowance_drop(org_id) == 0
    await store.allowance_add(org_id, 7, 100, 60_000)
    assert await store.allowance_take(org_id, 40) == 7
    assert await store.allowance_drop(org_id) == 60
    assert await store.allowance_take(org_id, 1) == 0
    assert await store.allowance_give_back(org_id, 7, 40) is False
    assert await store.allowance_add(org_id, 7, 0, 60_000) == 0


async def test_a_store_fault_reads_as_unavailable_and_takes_nothing(monkeypatch):
    faults = []
    monkeypatch.setattr(kv, '_note_fault', faults.append)

    async def broken(*args):
        raise ConnectionError('down')

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=broken)
    assert await store.allowance_take(5, 100) is None
    assert await store.allowance_give_back(5, 7, 100) is None
    assert await store.allowance_add(5, 7, 100, 1000) is None
    assert await store.allowance_drop(5) is None
    assert len(faults) == 4


async def test_arguments_that_are_not_plain_amounts_never_reach_the_script():
    calls = []

    async def record(*args):
        calls.append(args)
        return 1

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=record)
    for bad in (0, -1, 1.5, '1', True):
        with pytest.raises(ValueError):
            await store.allowance_take(5, bad)
    with pytest.raises(ValueError):
        await store.allowance_give_back(5, 7, -1)
    assert not calls
    assert await store.allowance_take(5, 100) == 1
    assert calls == [(kv._ALLOWANCE_TAKE, 1, 'treg:money-allowance:v1:5', 100)]


async def test_a_local_store_never_claims_to_be_the_shared_counter():
    store = kv.LocalStore()
    assert await store.allowance_take(5, 100) is None
    assert await store.allowance_give_back(5, 7, 100) is None
    assert await store.allowance_add(5, 7, 100, 1000) is None
    assert await store.allowance_drop(5) is None
