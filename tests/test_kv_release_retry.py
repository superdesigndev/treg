"""Bounded lease renewal and cleanup preserve ownership when a Redis reply is lost."""

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis import exceptions as redis_errors

from treg.infra import kv


@pytest.fixture(autouse=True)
def clear_diagnostics():
    kv.drain_lease_errors()
    kv.drain_lease_timings()
    yield
    kv.drain_lease_errors()
    kv.drain_lease_timings()


@pytest.fixture
async def local_redis():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    store = kv.RedisStore(url)
    key = f"treg-test:release-retry:{uuid4().hex}"
    try:
        assert await store.ping()
        yield store, key
    finally:
        await store._client.delete(key)
        await store.aclose()


def _store_with(command):
    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(set=command, eval=command)
    return store


async def _invoke_retry(store, phase, key="key", token="owner"):
    method = getattr(store, f"{phase}_lease")
    return await method(key, token, *((3000,) if phase == "renew" else ()))


async def test_acquire_allows_a_slow_reply_but_stops_at_its_own_deadline():
    calls = 0

    async def command(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.250)  # Beyond the former 200 ms outer deadline.
            return True
        await asyncio.Event().wait()

    store = _store_with(command)
    assert await store.acquire_lease("key", "owner", 3000) == "acquired"
    started = time.monotonic()
    assert await store.acquire_lease("other", "owner", 3000) == "unavailable"
    assert 1.4 <= time.monotonic() - started < 2.0
    assert calls == 2  # An ambiguous SET is never retried by the store.


@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_one_second_socket_failure_leaves_time_for_retry(phase):
    calls = []

    async def command(*args):
        calls.append(args)
        if len(calls) == 1:
            await asyncio.sleep(1.0)
            raise redis_errors.TimeoutError("socket read timed out")
        await asyncio.sleep(0.250)
        return 1

    assert await _invoke_retry(_store_with(command), phase) == {
        "renew": "renewed", "release": "released",
    }[phase]
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_default_retry_budget_is_two_and_a_half_seconds(phase):
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    started = time.monotonic()
    assert await _invoke_retry(_store_with(command), phase) == "unavailable"
    assert 2.4 <= time.monotonic() - started < 3.0
    assert calls == 2


async def test_minimum_lease_validity_cuts_short_the_second_renewal_attempt():
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    # A minimum three-second lease renewed at TTL/3 has at most two seconds left.
    started = time.monotonic()
    result = await _store_with(command).renew_lease(
        "key", "owner", 3000, deadline=started + 2.0)
    assert result == "deadline_expired"
    assert 1.9 <= time.monotonic() - started < 2.5
    assert calls == 2


@pytest.mark.parametrize("cancel", (False, True))
async def test_unobserved_real_acquire_is_cleaned_without_retry(local_redis, monkeypatch, cancel):
    from treg.infra import money_admission as admission

    real, _ = local_redis
    acquired = asyncio.Event()
    calls = 0

    async def uncertain_reply(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert await real._client.set(*args, **kwargs)
        acquired.set()
        await asyncio.Event().wait()

    store = _store_with(uncertain_reply)
    store._client.eval = real._client.eval
    monkeypatch.setattr(kv, "store", lambda: store)
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "_LEASE_ATTEMPT_TIMEOUT_S", 0.030)
    gate = admission._Gate(admission._Observation("close"), 3)
    task = asyncio.create_task(gate.acquire([int(uuid4().hex[:12], 16)], 1))
    try:
        await asyncio.wait_for(acquired.wait(), 1)
        key, token = gate.attempted[0]
        assert await real._client.get(key) == token
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == "kv_unavailable"
        assert calls == 1 and not gate.leases
        locks = list(gate.local)
        await admission._release_safely(gate)
        assert not await real._client.exists(key)
        assert all(not lock.locked() for lock in locks)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await admission._release_safely(gate)


async def test_lost_reply_after_real_delete_is_safe_to_retry(local_redis):
    real, key = local_redis
    assert await real.acquire_lease(key, "old-owner", 3000) == "acquired"
    calls = []

    async def uncertain_reply(*args):
        calls.append(args)
        result = await real._client.eval(*args)
        if len(calls) == 1:
            assert result == 1
            raise redis_errors.TimeoutError("reply lost after DEL")
        return result

    assert await _store_with(uncertain_reply).release_lease(key, "old-owner") == "not_owned"
    assert len(calls) == 2
    assert not await real._client.exists(key)
    assert calls[0] == calls[1]  # Both attempts use the original ownership token.


async def test_retry_cannot_delete_an_owner_created_after_old_lease_expired(local_redis):
    real, key = local_redis
    assert await real.acquire_lease(key, "old-owner", 3000) == "acquired"
    calls = 0

    async def expired_before_reply(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert await real._client.pexpire(key, 1)
            async with asyncio.timeout(1):
                while await real._client.exists(key):
                    await asyncio.sleep(0.001)
            assert await real.acquire_lease(key, "new-owner", 3000) == "acquired"
            raise redis_errors.ConnectionError("old connection dropped")
        return await real._client.eval(*args)

    assert await _store_with(expired_before_reply).release_lease(key, "old-owner") == "not_owned"
    assert calls == 2
    assert await real._client.get(key) == "new-owner"
    assert await real.renew_lease(key, "new-owner", 3000) == "renewed"


async def test_already_expired_real_key_is_lost_without_retry(local_redis):
    real, key = local_redis
    assert await real.acquire_lease(key, "expired-owner", 3000) == "acquired"
    assert await real._client.pexpire(key, 1)
    async with asyncio.timeout(1):
        while await real._client.exists(key):
            await asyncio.sleep(0.001)
    calls = 0

    async def observed_release(*args):
        nonlocal calls
        calls += 1
        return await real._client.eval(*args)

    assert await _store_with(observed_release).release_lease(key, "expired-owner") == "lost"
    assert calls == 1


async def test_renew_reply_lost_after_pexpire_retries_only_the_existing_owner(local_redis):
    real, key = local_redis
    assert await real.acquire_lease(key, "owner", 3000) == "acquired"
    calls = []

    async def lost_reply(*args):
        calls.append(args)
        result = await real._client.eval(*args)
        if len(calls) == 1:
            assert result == 1
            raise redis_errors.TimeoutError("reply lost after PEXPIRE")
        return result

    assert await _store_with(lost_reply).renew_lease(key, "owner", 3000) == "renewed"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert await real._client.get(key) == "owner"
    assert await real.acquire_lease(key, "competitor", 3000) == "busy"


@pytest.mark.parametrize("replace_owner", (False, True))
async def test_renew_retry_never_recreates_expired_key_or_extends_successor(local_redis, replace_owner):
    real, key = local_redis
    assert await real.acquire_lease(key, "old-owner", 3000) == "acquired"
    calls = 0
    successor_ttl = None

    async def expired_before_retry(*args):
        nonlocal calls, successor_ttl
        calls += 1
        if calls == 1:
            assert await real._client.pexpire(key, 1)
            async with asyncio.timeout(1):
                while await real._client.exists(key):
                    await asyncio.sleep(0.001)
            if replace_owner:
                assert await real.acquire_lease(key, "new-owner", 3000) == "acquired"
                successor_ttl = await real._client.pttl(key)
            raise redis_errors.ConnectionError("connection dropped")
        return await real._client.eval(*args)

    assert await _store_with(expired_before_retry).renew_lease(key, "old-owner", 30000) == "lost"
    assert calls == 2
    assert await real._client.get(key) == ("new-owner" if replace_owner else None)
    if replace_owner:
        assert 0 < await real._client.pttl(key) <= successor_ttl
    assert await real.release_lease(key, "old-owner") == "lost"


async def test_confirmed_renewal_ownership_loss_never_retries():
    calls = []

    async def no_owner(*args):
        calls.append(args)
        return 0

    assert await _store_with(no_owner).renew_lease("key", "owner", 3000) == "lost"
    assert len(calls) == 1


async def test_expired_renewal_deadline_sends_no_command():
    async def command(*args):
        pytest.fail("expired renewal must not send EVAL")

    result = await _store_with(command).renew_lease(
        "key", "owner", 3000, deadline=time.monotonic() - 1)
    assert result == "deadline_expired"
    assert kv.drain_lease_timings() == []
    assert kv.drain_lease_errors() == []


@pytest.mark.parametrize("fail_first", (False, True))
async def test_renewal_validity_timeout_is_not_a_redis_error(fail_first):
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        if fail_first and calls == 1:
            raise redis_errors.TimeoutError("first attempt failed")
        await asyncio.Event().wait()

    result = await _store_with(command).renew_lease(
        "key", "owner", 3000, deadline=time.monotonic() + 0.020)
    assert result == "deadline_expired"
    assert calls == 1 + fail_first
    errors = kv.drain_lease_errors()
    assert [(row["error_type"], row["count"]) for row in errors] == (
        [("redis_timeout", 1)] if fail_first else [])
    timings = {row["outcome"]: row for row in kv.drain_lease_timings()}
    assert set(timings) == ({"unavailable", "deadline_expired"} if fail_first else {"deadline_expired"})
    row = timings["deadline_expired"]
    assert row["outcome"] == "deadline_expired" and row["count"] == 1
    assert row["retry_count"] == int(fail_first)


async def test_process_death_leaves_only_the_remaining_ttl(local_redis):
    real, key = local_redis
    script = """
import asyncio, os, sys
from treg.infra.kv import RedisStore
async def own_then_exit():
    store = RedisStore(sys.argv[1])
    result = await store.acquire_lease(sys.argv[2], "dead-owner", 1500)
    os._exit(0 if result == "acquired" else 1)
asyncio.run(own_then_exit())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, os.environ["TREG_TEST_KV_URL"], key)
    try:
        assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await real.acquire_lease(key, "successor", 3000) == "busy"
        async with asyncio.timeout(3):
            while await real.acquire_lease(key, "successor", 3000) == "busy":
                await asyncio.sleep(0.01)
        assert await real._client.get(key) == "successor"
        assert await real.renew_lease(key, "dead-owner", 30000) == "lost"
        assert await real.release_lease(key, "dead-owner") == "lost"
        assert await real._client.get(key) == "successor"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("error_type", (
    TimeoutError, redis_errors.TimeoutError, redis_errors.ConnectionError,
))
@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_one_transient_failure_is_retried_with_same_owner(error_type, phase):
    calls = []

    async def command(*args):
        calls.append(args)
        if len(calls) == 1:
            raise error_type("transient")
        return 1

    result = await _invoke_retry(_store_with(command), phase, "private-key", "private-token")
    assert result == {"renew": "renewed", "release": "released"}[phase]
    assert len(calls) == 2
    assert calls[0] == calls[1]
    row, = kv.drain_lease_errors()
    assert row["phase"] == phase and row["count"] == 1
    timings = {row["outcome"]: row for row in kv.drain_lease_timings()}
    assert timings[result]["retry_count"] == 1
    assert timings["unavailable"]["count"] == 1


@pytest.mark.parametrize("error_type", (
    redis_errors.AuthenticationError, redis_errors.AuthorizationError,
    redis_errors.NoPermissionError, redis_errors.ResponseError, RuntimeError,
))
@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_permanent_error_does_not_retry(error_type, phase):
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        raise error_type("do not retry")

    assert await _invoke_retry(_store_with(command), phase) == "unavailable"
    assert calls == 1
    row, = kv.drain_lease_errors()
    assert row["count"] == 1


@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_two_transient_failures_stop_after_second_attempt(phase):
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        raise redis_errors.TimeoutError("still unavailable")

    assert await _invoke_retry(_store_with(command), phase) == "unavailable"
    assert calls == 2
    row, = kv.drain_lease_errors()
    assert row["phase"] == phase and row["count"] == 2
    timing, = kv.drain_lease_timings()
    assert timing["phase"] == phase and timing["outcome"] == "unavailable"
    assert timing["count"] == 2 and timing["retry_count"] == 1


@pytest.mark.parametrize("cancel_attempt", (1, 2))
@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_cancellation_propagates_and_never_starts_another_attempt(cancel_attempt, phase):
    entered = asyncio.Event()
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        if calls < cancel_attempt:
            raise redis_errors.ConnectionError("first attempt failed")
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_invoke_retry(_store_with(command), phase))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == cancel_attempt
        assert sum(row["count"] for row in kv.drain_lease_errors()) == cancel_attempt - 1
        cancelled, = [r for r in kv.drain_lease_timings() if r["outcome"] == "cancelled"]
        assert cancelled["count"] == 1 and cancelled["retry_count"] == cancel_attempt - 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_retry_shares_total_budget_with_first_attempt(monkeypatch, phase):
    monkeypatch.setattr(kv, "_LEASE_ATTEMPT_TIMEOUT_S", 0.060)
    monkeypatch.setattr(kv, "_LEASE_RETRY_BUDGET_S", 0.100)
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    started = asyncio.get_running_loop().time()
    assert await _invoke_retry(_store_with(command), phase) == "unavailable"
    elapsed = asyncio.get_running_loop().time() - started
    assert calls == 2
    assert 0.080 <= elapsed < 0.200
    row, = kv.drain_lease_errors()
    assert row["count"] == 2 and row["error_type"] == "deadline_exceeded"


@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_first_attempt_cleanup_cannot_grant_second_attempt_a_fresh_budget(monkeypatch, phase):
    monkeypatch.setattr(kv, "_LEASE_ATTEMPT_TIMEOUT_S", 0.100)
    monkeypatch.setattr(kv, "_LEASE_RETRY_BUDGET_S", 0.200)
    loop = asyncio.get_running_loop()
    attempts = []

    async def command(*args):
        attempts.append(loop.time())
        if len(attempts) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A timed-out driver can take time to close its connection. That time must
                # come out of the original per-key budget, not buy the next attempt 100ms.
                await asyncio.sleep(0.070)
                raise redis_errors.TimeoutError("timed-out connection cleanup finished")
        await asyncio.Event().wait()

    started = loop.time()
    assert await _invoke_retry(_store_with(command), phase) == "unavailable"
    finished = loop.time()
    assert len(attempts) == 2
    assert attempts[1] - started >= 0.160
    assert finished - started < 0.245
    assert finished - attempts[1] < 0.075


@pytest.mark.parametrize("phase", ("renew", "release"))
async def test_exhausted_total_budget_does_not_start_a_second_command(monkeypatch, phase):
    monkeypatch.setattr(kv, "_LEASE_ATTEMPT_TIMEOUT_S", 0.020)
    monkeypatch.setattr(kv, "_LEASE_RETRY_BUDGET_S", 0.040)
    calls = 0

    async def command(*args):
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.030)
            raise redis_errors.TimeoutError("cleanup used remaining budget")

    assert await _invoke_retry(_store_with(command), phase) == "unavailable"
    assert calls == 1


@pytest.mark.parametrize(("phase", "result", "outcome"), (
    ("acquire", True, "acquired"), ("acquire", False, "busy"),
    ("renew", True, "renewed"), ("renew", False, "lost"),
    ("release", True, "released"), ("release", False, "lost"),
))
async def test_timings_aggregate_all_command_outcomes_into_disjoint_buckets(
    monkeypatch, phase, result, outcome,
):
    clock = [0.0]
    elapsed = iter((0.0, 0.008, 0.025, 0.075, 0.125, 0.250))
    monkeypatch.setattr(kv, "_clock", lambda: clock[0])

    async def command(*args, **kwargs):
        clock[0] += next(elapsed)
        return result

    store = _store_with(command)
    method = getattr(store, f"{phase}_lease")
    args = ("sensitive-key", "sensitive-token") + (() if phase == "release" else (3000,))
    for _ in range(6):
        assert await method(*args) == outcome
    row, = kv.drain_lease_timings()
    assert row == {
        "event": "kv_lease_gauge", "phase": phase, "outcome": outcome,
        "count": 6, "retry_count": 0, "elapsed_total_ms": pytest.approx(483),
        "elapsed_max_ms": pytest.approx(250), "bucket_le_10_ms": 2, "bucket_le_50_ms": 1,
        "bucket_le_100_ms": 1, "bucket_le_200_ms": 1, "bucket_gt_200_ms": 1,
    }
    assert "sensitive" not in repr(row)
    assert kv.drain_lease_timings() == []


async def test_retry_timing_is_per_attempt_and_drain_starts_a_new_window(monkeypatch):
    clock = [0.0]
    calls = 0
    monkeypatch.setattr(kv, "_clock", lambda: clock[0])

    async def command(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            clock[0] += 0.075
            raise redis_errors.TimeoutError("private-token")
        clock[0] += 0.008
        return 0

    store = _store_with(command)
    assert await store.release_lease("private-key", "private-token") == "not_owned"
    rows = {row["outcome"]: row for row in kv.drain_lease_timings()}
    assert set(rows) == {"unavailable", "not_owned"}
    assert rows["unavailable"]["count"] == 1 and rows["unavailable"]["retry_count"] == 0
    assert rows["unavailable"]["elapsed_total_ms"] == pytest.approx(75)
    assert rows["not_owned"]["count"] == 1 and rows["not_owned"]["retry_count"] == 1
    assert rows["not_owned"]["elapsed_total_ms"] == pytest.approx(8)
    assert await store.release_lease("private-key", "private-token") == "lost"
    next_row, = kv.drain_lease_timings()
    assert next_row["count"] == 1 and next_row["retry_count"] == 0
    assert rows["not_owned"]["count"] == 1
