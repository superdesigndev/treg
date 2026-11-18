"""A team's allowance counter in the shared store: one figure every process spends from."""
import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from treg.infra import kv

def _soon(ms: int = 60_000) -> int:
    return int(time.time() * 1000) + ms


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
    assert await store.allowance_add(org_id, 7, 250, until_ms=_soon(), ttl_ms=60_000) == 250
    assert [await store.allowance_take(org_id, 100) for _ in range(3)] == [7, 7, 0]
    assert await store.allowance_take(org_id, 50) == 7
    assert await store.allowance_take(org_id, 1) == 0
    assert 0 < await store._client.pttl(kv._allowance_key(org_id)) <= 60_000


@real_redis
async def test_takes_stop_once_the_counter_is_no_longer_fresh_and_resume_on_the_next_add(counter):
    store, org_id = counter
    assert await store.allowance_available(org_id) == 0
    await store.allowance_add(org_id, 7, 100, until_ms=_soon(40), ttl_ms=60_000)
    assert await store.allowance_take(org_id, 10) == 7
    await asyncio.sleep(0.08)
    assert await store.allowance_take(org_id, 10) == 0
    assert await store.allowance_available(org_id) == 90
    assert await store.allowance_give_back(org_id, 7, 10) is True
    assert await store.allowance_add(org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000) == 100
    assert await store.allowance_take(org_id, 100) == 7


@real_redis
async def test_an_add_that_arrives_late_never_moves_the_deadline_back_or_reopens_it(counter):
    store, org_id = counter
    await store.allowance_add(org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000)
    assert await store.allowance_add(org_id, 7, 50, until_ms=_soon(-5_000), ttl_ms=60_000) == 150
    assert await store.allowance_take(org_id, 10) == 7, "the later deadline stands"
    await store.allowance_retire(org_id, 7, 60_000)
    assert await store.allowance_add(org_id, 8, 100, until_ms=_soon(-5_000), ttl_ms=60_000) == 100
    assert await store.allowance_take(org_id, 10) == 0, "published after its own deadline"


@real_redis
async def test_take_up_to_takes_what_is_there_from_its_own_epoch_only(counter):
    store, org_id = counter
    await store.allowance_add(org_id, 7, 400, until_ms=_soon(), ttl_ms=60_000)
    assert await store.allowance_take_up_to(org_id, 8, 900) == 0
    assert await store.allowance_take_up_to(org_id, 7, 150) == 150
    assert await store.allowance_take_up_to(org_id, 7, 900) == 250
    assert await store.allowance_take_up_to(org_id, 7, 900) == 0
    assert await store.allowance_available(org_id) == 0


@real_redis
async def test_concurrent_takes_from_two_clients_never_overspend(counter):
    store, org_id = counter
    other = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    try:
        await store.allowance_add(org_id, 3, 1_000, until_ms=_soon(), ttl_ms=60_000)
        taken = await asyncio.gather(*(
            client.allowance_take(org_id, 30) for _ in range(40) for client in (store, other)))
    finally:
        await other.aclose()
    assert sum(1 for epoch in taken if epoch == 3) == 33 and set(taken) == {0, 3}
    assert await store.allowance_drop(org_id) == 10


@real_redis
async def test_a_give_back_reaches_only_the_counter_it_was_taken_from(counter):
    store, org_id = counter
    await store.allowance_add(org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000)
    assert await store.allowance_take(org_id, 100) == 7
    assert await store.allowance_give_back(org_id, 7, 30) is True
    assert await store.allowance_give_back(org_id, 8, 30) is False
    assert await store.allowance_add(org_id, 7, 20, until_ms=_soon(), ttl_ms=60_000) == 50
    assert await store.allowance_add(org_id, 9, 500, until_ms=_soon(), ttl_ms=60_000) == 500
    assert await store.allowance_give_back(org_id, 7, 30) is False
    assert await store.allowance_take(org_id, 500) == 9


@real_redis
async def test_dropping_empties_the_counter_and_keeps_its_epoch(counter):
    store, org_id = counter
    assert await store.allowance_drop(org_id) == 0
    await store.allowance_add(org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000)
    assert await store.allowance_take(org_id, 40) == 7
    assert await store.allowance_drop(org_id) == 60
    assert await store.allowance_drop(org_id) == 0
    assert await store.allowance_take(org_id, 1) == 0
    assert await store.allowance_give_back(org_id, 7, 40) is True
    assert await store.allowance_add(org_id, 7, 10, until_ms=_soon(), ttl_ms=60_000) == 50


@real_redis
async def test_a_writer_with_an_older_epoch_never_replaces_a_newer_counter(counter):
    store, org_id = counter
    await store.allowance_add(org_id, 9, 500, until_ms=_soon(), ttl_ms=60_000)
    assert await store.allowance_add(org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000) == 'stale'
    assert await store.allowance_give_back(org_id, 7, 100) is False
    assert await store.allowance_retire(org_id, 7, 60_000) == 0
    assert await store.allowance_take(org_id, 500) == 9


@real_redis
async def test_retiring_a_counter_refuses_its_late_writers_and_admits_the_next_hold(counter):
    store, org_id = counter
    epoch = 1_800_000_000_000_007  # the size of a real one: past what a script prints exactly
    await store.allowance_add(org_id, epoch, 100, until_ms=_soon(), ttl_ms=60_000)
    assert await store.allowance_take(org_id, 40) == epoch
    assert await store.allowance_retire(org_id, epoch, 60_000) == 60
    assert await store.allowance_add(org_id, epoch, 100, until_ms=_soon(), ttl_ms=60_000) == 'stale'
    assert await store.allowance_give_back(org_id, epoch, 40) is False
    assert await store.allowance_take(org_id, 1) == 0
    assert 0 < await store._client.pttl(kv._allowance_key(org_id)) <= 60_000
    assert await store.allowance_add(org_id, epoch + 1, 30, until_ms=_soon(), ttl_ms=60_000) == 30
    assert await store.allowance_take(org_id, 30) == epoch + 1


@real_redis
async def test_retiring_with_no_counter_still_leaves_the_fence(counter):
    store, org_id = counter
    assert await store.allowance_retire(org_id, 7, 60_000) == 0
    assert await store.allowance_add(org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000) == 'stale'


async def test_a_store_fault_reads_as_unavailable_and_takes_nothing(monkeypatch):
    faults = []
    monkeypatch.setattr(kv, '_note_fault', faults.append)

    async def broken(*args):
        raise ConnectionError('down')

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=broken)
    assert await store.allowance_take(5, 100) is None
    assert await store.allowance_give_back(5, 7, 100) is None
    assert await store.allowance_add(5, 7, 100, until_ms=_soon(), ttl_ms=1000) is None
    assert await store.allowance_drop(5) is None
    assert await store.allowance_retire(5, 7, 1000) is None
    assert await store.allowance_available(5) is None
    assert await store.allowance_take_up_to(5, 7, 100) is None
    assert len(faults) == 7


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
    assert await store.allowance_add(5, 7, 100, until_ms=_soon(), ttl_ms=1000) is None
    assert await store.allowance_drop(5) is None
    assert await store.allowance_retire(5, 7, 1000) is None
    assert await store.allowance_available(5) is None
    assert await store.allowance_take_up_to(5, 7, 100) is None
