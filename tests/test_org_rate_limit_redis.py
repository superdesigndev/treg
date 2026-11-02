"""Atomic published rate/concurrency admission against disposable loopback Redis."""
import asyncio
import json
import math
import os
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from treg.infra import kv


def _keys(org_id):
    return f"treg:org-rate:v1:{org_id}", f"treg:org-concurrency:v1:{org_id}"


async def _ready(store):
    # Retry fixture readiness only; admission itself must keep its production timeout/no-retry.
    try:
        async with asyncio.timeout(5):
            while not await store.ping():
                await asyncio.sleep(0.05)
            # Open the pool's connections up front so a concurrent burst spends admission's
            # short timeout on Redis, not on connecting.
            await asyncio.gather(*(store._client.ping() for _ in range(20)))
    except TimeoutError as exc:
        raise AssertionError("disposable Redis did not become ready within five seconds") from exc


@pytest.fixture
async def stores():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    org_id = uuid4().int % 2147483646 + 1
    other_org = org_id + 1
    clients = []
    try:
        clients.append(kv.RedisStore(url))
        clients.append(kv.RedisStore(url))
        await asyncio.gather(*(_ready(store) for store in clients))
        try:
            yield clients[0], clients[1], org_id, other_org
        finally:
            await clients[0]._client.delete(*_keys(org_id), *_keys(other_org))
    finally:
        await asyncio.gather(*(store.aclose() for store in clients))


async def _now_us(store):
    seconds, micros = await store._client.time()
    return int(seconds) * 1_000_000 + int(micros)


async def _seed(store, org_id, *, tokens, rps, burst, age_s=0, future_s=0):
    now = await _now_us(store)
    await store._client.hset(_keys(org_id)[0], mapping={
        "tokens": tokens, "rps": rps, "burst": burst,
        "last_us": now - round(age_s * 1_000_000) + round(future_s * 1_000_000),
    })


async def _admit(store, org_id, token, *, limit=3, rps=5e-324, burst=3):
    return await store.admit_org(org_id, token, 30_000, limit, rps=rps, burst=burst)


async def test_one_shared_burst_across_clients_processes_and_independent_orgs(stores):
    first, second, org_id, other_org = stores
    results = await asyncio.gather(*(
        _admit(first if index % 2 else second, org_id, f"owner-{index}", limit=None)
        for index in range(20)
    ))
    assert [result.outcome for result in results].count("acquired") == 3
    assert [result.outcome for result in results].count("rate_limited") == 17
    assert all(result.retry_after_s == 0 for result in results if result.outcome == "acquired")
    assert all(result.retry_after_s == 2147483647
               for result in results if result.outcome == "rate_limited")
    assert await first._client.exists(_keys(org_id)[1]) == 0

    script = """
import asyncio, json, sys
from treg.infra.kv import RedisStore
async def main():
    store = RedisStore(sys.argv[1])
    try:
        result = await store.admit_org(int(sys.argv[2]), 'another-process', 30000, None,
                                      rps=5e-324, burst=3)
        print(json.dumps([result.outcome, result.retry_after_s]))
    finally:
        await store.aclose()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, os.environ["TREG_TEST_KV_URL"], str(org_id),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout) == ["rate_limited", 2147483647]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert (await _admit(second, other_org, "independent-org", limit=None)).outcome == "acquired"


async def test_fractional_refill_consumes_one_token_and_returns_positive_retry(stores):
    first, _, org_id, _ = stores
    await _seed(first, org_id, tokens=0, rps=0.125, burst=3, age_s=12)
    assert (await _admit(first, org_id, "first", limit=None, rps=0.125)).outcome == "acquired"
    balance = float(await first._client.hget(_keys(org_id)[0], "tokens"))
    assert 0.5 <= balance < 0.9
    denied = await _admit(first, org_id, "second", limit=None, rps=0.125)
    assert denied.outcome == "rate_limited"
    assert 1 <= denied.retry_after_s <= 4
    assert await first._client.exists(_keys(org_id)[1]) == 0


async def test_fractional_balance_just_below_one_is_not_rounded_into_a_free_token(stores):
    first, second, org_id, _ = stores
    rate_key, _ = _keys(org_id)
    balance = math.nextafter(1.0, 0.0)
    await _seed(first, org_id, tokens=balance, rps=1, burst=2, future_s=30)
    for store in (first, second):
        assert (await _admit(store, org_id, "below-one", limit=None, rps=1, burst=2)).outcome == "rate_limited"
        assert float(await first._client.hget(rate_key, "tokens")) == balance


async def test_concurrency_contention_does_not_spend_a_rate_token(stores):
    first, second, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    assert await first.acquire_slot(slot_key, "existing-concurrency-owner", 30_000, 1) == "acquired"
    denied = await _admit(second, org_id, "busy-owner", limit=1)
    assert (denied.outcome, denied.retry_after_s) == ("busy", 1)
    assert float(await first._client.hget(rate_key, "tokens")) == 3
    assert await first._client.zrange(slot_key, 0, -1) == ["existing-concurrency-owner"]
    assert await first.release_slot(slot_key, "existing-concurrency-owner") == "released"
    assert (await _admit(second, org_id, "replacement", limit=1)).outcome == "acquired"
    assert float(await first._client.hget(rate_key, "tokens")) == 2


async def test_rate_refusal_neither_allocates_nor_erases_slot_owners(stores):
    first, second, org_id, _ = stores
    _, slot_key = _keys(org_id)
    await _seed(first, org_id, tokens=0, rps=5e-324, burst=1)
    assert await first.acquire_slot(slot_key, "admitted-before-rate", 30_000, 5) == "acquired"
    denied = await _admit(second, org_id, "rate-refused", limit=5, burst=1)
    assert denied.outcome == "rate_limited"
    assert denied.retry_after_s == 2147483647
    assert await first._client.zrange(slot_key, 0, -1) == ["admitted-before-rate"]


async def test_atomic_mixed_admission_counts_only_accepted_requests(stores):
    first, second, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    results = await asyncio.gather(*(
        _admit(first if index % 2 else second, org_id, f"mixed-{index}", limit=3, burst=100)
        for index in range(30)
    ))
    outcomes = [result.outcome for result in results]
    assert outcomes.count("acquired") == 3
    assert outcomes.count("busy") == 27
    assert float(await first._client.hget(rate_key, "tokens")) == 97
    assert await first._client.zcard(slot_key) == 3


async def test_rate_only_admission_does_not_touch_existing_slot_state(stores):
    first, _, org_id, _ = stores
    _, slot_key = _keys(org_id)
    await first._client.set(slot_key, "synthetic incompatible legacy state")
    assert (await _admit(first, org_id, "rate-only", limit=None)).outcome == "acquired"
    assert await first._client.get(slot_key) == "synthetic incompatible legacy state"


async def test_old_rate_is_used_for_elapsed_refill_before_applying_new_parameters(stores):
    first, _, org_id, _ = stores
    rate_key, _ = _keys(org_id)
    await _seed(first, org_id, tokens=0, rps=0.25, burst=4, age_s=4)
    assert (await _admit(first, org_id, "new-rate", limit=None, rps=100, burst=10)).outcome == "acquired"
    state = await first._client.hgetall(rate_key)
    assert 0 <= float(state["tokens"]) < 0.25
    assert float(state["rps"]) == 100
    assert int(state["burst"]) == 10


async def test_increasing_capacity_and_stale_parameters_do_not_create_a_fresh_burst(stores):
    first, second, org_id, _ = stores
    rate_key, _ = _keys(org_id)
    # A future timestamp freezes accrual independently of scheduler/CI timing.
    await _seed(first, org_id, tokens=0.25, rps=0.5, burst=2, future_s=30)
    increased = await _admit(first, org_id, "new-parameters", limit=None, rps=100, burst=10)
    assert increased.outcome == "rate_limited"
    assert float(await first._client.hget(rate_key, "tokens")) == 0.25
    stale = await _admit(second, org_id, "cached-old-parameters", limit=None, rps=0.5, burst=2)
    assert stale.outcome == "rate_limited"
    state = await first._client.hgetall(rate_key)
    assert float(state["tokens"]) == 0.25
    assert float(state["rps"]) == 0.5
    assert int(state["burst"]) == 2


async def test_decreasing_capacity_clamps_existing_tokens_without_restarting_bucket(stores):
    first, second, org_id, _ = stores
    rate_key, _ = _keys(org_id)
    await _seed(first, org_id, tokens=8, rps=1, burst=10, future_s=30)
    assert (await _admit(first, org_id, "lower-burst", limit=None, rps=2, burst=3)).outcome == "acquired"
    assert float(await first._client.hget(rate_key, "tokens")) == 2
    assert (await _admit(second, org_id, "old-burst", limit=None, rps=1, burst=10)).outcome == "acquired"
    assert float(await first._client.hget(rate_key, "tokens")) == 1


async def test_busy_request_updates_parameters_without_debiting_balance(stores):
    first, _, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    await _seed(first, org_id, tokens=8, rps=1, burst=10, future_s=30)
    assert await first.acquire_slot(slot_key, "existing", 30_000, 1) == "acquired"
    result = await _admit(first, org_id, "busy-new-rule", limit=1, rps=2, burst=3)
    assert result.outcome == "busy"
    state = await first._client.hgetall(rate_key)
    assert float(state["tokens"]) == 3
    assert float(state["rps"]) == 2
    assert int(state["burst"]) == 3


async def test_config_changes_keep_existing_owners_and_released_rate_tokens_are_not_refunded(stores):
    first, second, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    assert await first.acquire_slot(slot_key, "old-path", 30_000, 2) == "acquired"
    assert (await _admit(second, org_id, "new-path", limit=2, burst=2)).outcome == "acquired"
    assert (await _admit(first, org_id, "lower-limit", limit=1, burst=2)).outcome == "busy"
    assert set(await first._client.zrange(slot_key, 0, -1)) == {"old-path", "new-path"}
    assert await second.release_slot(slot_key, "new-path") == "released"
    assert float(await first._client.hget(rate_key, "tokens")) == 1
    assert (await _admit(first, org_id, "rate-only", limit=None, burst=2)).outcome == "acquired"
    assert await first._client.zrange(slot_key, 0, -1) == ["old-path"]
    assert (await _admit(first, org_id, "depleted", limit=3, burst=2)).outcome == "rate_limited"
    assert await first._client.zrange(slot_key, 0, -1) == ["old-path"]


async def test_atomic_slot_admission_expires_abandoned_owners_without_shortening_live_lease(stores):
    first, second, org_id, _ = stores
    _, slot_key = _keys(org_id)
    assert await first.acquire_slot(slot_key, "long-owner", 60_000, 3) == "acquired"
    await first._client.zadd(slot_key, {"abandoned-owner": 1})
    assert (await _admit(second, org_id, "new-owner", limit=2)).outcome == "acquired"
    assert set(await first._client.zrange(slot_key, 0, -1)) == {"long-owner", "new-owner"}
    assert await first._client.pttl(slot_key) > 50_000


async def test_bucket_expires_only_after_full_refill_and_tiny_rates_persist(stores):
    first, _, org_id, other_org = stores
    assert (await _admit(first, org_id, "ordinary", limit=None, rps=0.1, burst=3)).outcome == "acquired"
    # One spent token at 0.1/s needs ten seconds, rather than a burst-size window reset.
    assert 8000 <= await first._client.pttl(_keys(org_id)[0]) <= 10_100
    assert (await _admit(first, other_org, "tiny", limit=None, rps=5e-324, burst=3)).outcome == "acquired"
    assert await first._client.pttl(_keys(other_org)[0]) == -1


async def test_backward_clock_does_not_refill_early_or_expire_bucket_before_recovery(stores):
    first, _, org_id, _ = stores
    rate_key, _ = _keys(org_id)
    await _seed(first, org_id, tokens=0, rps=1, burst=1, future_s=300)
    previous = int(await first._client.hget(rate_key, "last_us"))
    result = await _admit(first, org_id, "clock-went-back", limit=None, rps=1, burst=1)
    assert result.outcome == "rate_limited"
    assert 299 <= result.retry_after_s <= 301
    state = await first._client.hgetall(rate_key)
    assert float(state["tokens"]) == 0
    assert int(state["last_us"]) == previous
    assert await first._client.pttl(rate_key) >= 298_000


@pytest.mark.parametrize("bad", ["string", "list"])
async def test_wrong_rate_key_type_allows_without_touching_slots(stores, bad):
    first, _, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    if bad == "string":
        await first._client.set(rate_key, "synthetic wrong type")
    else:
        await first._client.lpush(rate_key, "synthetic wrong type")
    await first._client.zadd(slot_key, {"expired-owner": 1})
    before_rate, before_slots = await first._client.dump(rate_key), await first._client.dump(slot_key)
    result = await _admit(first, org_id, "must-not-write")
    assert (result.outcome, result.retry_after_s) == ("unavailable", 0)
    assert await first._client.dump(rate_key) == before_rate
    assert await first._client.dump(slot_key) == before_slots


@pytest.mark.parametrize("bad", [
    {"tokens": "nan"}, {"tokens": "inf"}, {"tokens": -1}, {"tokens": 4},
    {"last_us": "not-a-time"}, {"last_us": -1},
    {"rps": 0}, {"rps": "nan"}, {"rps": "inf"},
    {"burst": 0}, {"burst": 1.5}, {"extra": "unexpected"},
])
async def test_invalid_bucket_state_is_unavailable_without_partial_mutations(stores, bad):
    first, _, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    await _seed(first, org_id, tokens=1, rps=1, burst=3)
    await first._client.hset(rate_key, mapping=bad)
    await first._client.zadd(slot_key, {"expired-owner": 1})
    before_rate, before_slots = await first._client.dump(rate_key), await first._client.dump(slot_key)
    assert (await _admit(first, org_id, "must-not-write")).outcome == "unavailable"
    assert await first._client.dump(rate_key) == before_rate
    assert await first._client.dump(slot_key) == before_slots


async def test_incomplete_bucket_is_not_reset_to_a_fresh_burst(stores):
    first, _, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    await first._client.hset(rate_key, mapping={"tokens": 0})
    before = await first._client.dump(rate_key)
    assert (await _admit(first, org_id, "incomplete")).outcome == "unavailable"
    assert await first._client.dump(rate_key) == before
    assert await first._client.exists(slot_key) == 0


async def test_wrong_slot_type_does_not_debit_an_existing_bucket(stores):
    first, _, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    await _seed(first, org_id, tokens=2, rps=5e-324, burst=3)
    await first._client.set(slot_key, "synthetic wrong type")
    before = await first._client.dump(rate_key)
    assert (await _admit(first, org_id, "must-not-spend")).outcome == "unavailable"
    assert await first._client.dump(rate_key) == before
    assert await first._client.get(slot_key) == "synthetic wrong type"


@pytest.mark.parametrize("score", [float("inf"), float("-inf"), -1, 0.5])
async def test_invalid_slot_deadline_does_not_initialize_or_debit_a_bucket(stores, score):
    first, _, org_id, _ = stores
    rate_key, slot_key = _keys(org_id)
    await first._client.zadd(slot_key, {"invalid-owner": score})
    before = await first._client.dump(slot_key)
    assert (await _admit(first, org_id, "must-not-spend")).outcome == "unavailable"
    assert await first._client.exists(rate_key) == 0
    assert await first._client.dump(slot_key) == before


async def test_rate_admission_reports_local_store_and_outage_as_unavailable():
    store = kv.RedisStore("redis://127.0.0.1:1")
    try:
        for backend in (store, kv.LocalStore()):
            result = await _admit(backend, 42, "synthetic")
            assert (result.outcome, result.retry_after_s) == ("unavailable", 0)
    finally:
        await store.aclose()


@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_admission_timeout_and_error_do_not_retry_or_expose_payload(monkeypatch, caplog, failure):
    calls = 0

    async def evaluate(*_args):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("redis://private.invalid secret-owner raw-config")

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=evaluate)
    monkeypatch.setattr(kv, "_OPTIONAL_TIMEOUT_S", 0.02)
    result = await asyncio.wait_for(_admit(store, 42, "secret-owner"), 1)
    assert (result.outcome, result.retry_after_s) == ("unavailable", 0)
    assert calls == 1
    assert not any(value in caplog.text for value in ("private.invalid", "secret-owner", "raw-config"))


async def test_admission_cancellation_propagates_without_retry():
    entered = asyncio.Event()
    calls = 0

    async def evaluate(*_args):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=evaluate)
    task = asyncio.create_task(_admit(store, 42, "cancelled-owner"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1
