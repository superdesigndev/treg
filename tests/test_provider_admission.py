"""Shared provider admission against disposable Redis, never a developer's configured KV.

Run with TREG_TEST_KV_URL (CI's Postgres job supplies its scratch Redis). Independent clients
and spawned processes exercise the actual Lua and lifecycle, not a process-local Redis mock.
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
import uuid
from threading import Event
from types import SimpleNamespace

import pytest

from treg.config import get_settings
from treg.domain.capacity.fair_share import FairShare, ShareBusy, fair_share
from treg.infra import kv, provider_store
from treg.infra.upstream.limiter import Limiter
from treg.application.call import service
from treg.application.call.types import UpstreamResponse

from test_marketplace_call import EP, _balance, _fake_relay, platform_on  # noqa: F401
from test_call_response_limits import FETCH, wire  # noqa: F401


@pytest.fixture
async def provider_kv(monkeypatch):
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable Redis via TREG_TEST_KV_URL")
    first, second = kv.RedisStore(url), kv.RedisStore(url)
    assert await first.ping()
    monkeypatch.setenv("TREG_KV_URL", url)
    get_settings.cache_clear()
    monkeypatch.setattr(kv, "_store", first)
    pool = f"test-provider-{uuid.uuid4().hex}"
    yield first, second, pool
    await fair_share.aclose()
    # Delete this test's namespace only; parallel workers share the disposable Redis database.
    prefix = provider_store.pool_keys(pool)[0].split("}", 1)[0] + "}:*"
    keys = [key async for key in first._client.scan_iter(match=prefix)]
    if keys:
        await first._client.delete(*keys)
    await first.aclose()
    await second.aclose()
    get_settings.cache_clear()


async def test_clients_share_cap_and_team_fairness(provider_kv):
    one, two, pool = provider_kv
    a, b = one.provider_capacity(), two.provider_capacity()
    for i in range(3):
        assert (await a.claim([pool], f"1:heavy{i}", 3, 1000))[1] == "acquired"
    assert (await a.claim([pool], "1:waiting", 3, 1000))[1] == "waiting"
    assert (await b.claim([pool], "2:light", 3, 1000))[1] == "waiting"
    await a.abandon([pool], "1:heavy0", 3)
    statuses = await b.update(pool, 3, waiting=["1:waiting", "2:light"])
    assert statuses["2:light"] == ("acquired", 1)
    assert statuses["1:waiting"][0] == "waiting"
    assert await one._client.zcard(provider_store.pool_keys(pool)[0]) == 3
    # Releasing an old or unrelated owner never frees a different team's place.
    await a.abandon([pool], "1:heavy0", 3)
    await a.abandon([pool], "2:foreign", 3)
    assert await one._client.zcard(provider_store.pool_keys(pool)[0]) == 3


async def test_expiry_prunes_team_counts_and_stale_owner_cannot_renew_or_release_successor(provider_kv):
    one, two, pool = provider_kv
    a, b = one.provider_capacity(), two.provider_capacity()
    assert (await a.claim([pool], "1:old", 1, 1000, lease_ms=80))[1] == "acquired"
    await asyncio.sleep(0.1)
    assert (await b.claim([pool], "2:new", 1, 1000))[1] == "acquired"
    assert (await a.update(pool, 1, renew=["1:old"]))["1:old"][0] == "missing"
    await a.abandon([pool], "1:old", 1)
    keys = provider_store.pool_keys(pool)
    assert await one._client.zrange(keys[0], 0, -1) == ["2:new"]
    assert await one._client.hget(keys[1], "1") is None
    assert await one._client.hget(keys[1], "2") == "1"
    await b.abandon([pool], "2:new", 1)
    assert await one._client.exists(*keys) == 0


async def test_queue_is_bounded_and_abandoned_waiters_expire(provider_kv, monkeypatch):
    one, _, pool = provider_kv
    store = one.provider_capacity()
    monkeypatch.setattr(provider_store, "MAX_WAITERS", 2)
    await store.claim([pool], "1:held", 1, 1000)
    for token in ["2:wait", "3:wait"]:
        assert (await store.claim([pool], token, 1, 40))[1] == "waiting"
    assert (await store.claim([pool], "4:excess", 1, 40))[1] == "full"
    keys = provider_store.pool_keys(pool)
    assert await one._client.zcard(keys[2]) == 2
    assert all([await one._client.pttl(key) > 0 for key in keys])
    await asyncio.sleep(0.06)
    await store.abandon([pool], "1:held", 1)
    assert await one._client.exists(*keys) == 0


async def test_shared_rate_window_endpoint_and_account_isolation_and_soft_overflow(provider_kv):
    one, two, pool = provider_kv
    a, b = one.provider_capacity(), two.provider_capacity()
    row = [(pool, pool, 100)]  # ten per second, capacity one
    reservations = await asyncio.gather(*(s.reserve_rate(row, 0, 2000) for s in [a, b] * 5))
    waits = sorted(r[1] for r in reservations)
    assert waits[0] == 0 and waits[-1] >= 850
    assert all(y - x >= 85 for x, y in zip(waits, waits[1:])), waits
    # No burst reset at a window boundary; a reservation from another client sees the debt.
    assert (await b.reserve_rate(row, 0, 2000))[1] >= 950
    assert (await b.reserve_rate([(pool, pool + ".endpoint", 100)], 0, 2000))[1] == 0
    assert (await b.reserve_rate([(pool + "#b", pool + "#b", 100)], 0, 2000))[1] == 0
    key = provider_store.rate_key(pool, pool)
    before = await one._client.hgetall(key)
    assert (await a.reserve_rate(row, 0, 0))[1] == 0  # overflow proceeds, no reservation
    assert await one._client.hgetall(key) == before
    assert 0 < await one._client.pttl(key) <= 1200
    # A policy change resets the spacer just as the local bucket does.
    assert (await a.reserve_rate([(pool, pool, 200)], 0, 2000))[1] == 0


async def test_account_selection_and_rate_reservation_are_atomic(provider_kv):
    one, two, pool = provider_kv
    rows = [(pool, pool, 100), (pool + "#b", pool + "#b", 100)]
    results = await asyncio.gather(*(
        store.provider_capacity().reserve_rate(rows, 0, 2000) for store in [one, two] * 8))
    assert [r[0] for r in results].count(0) == 8
    assert [r[0] for r in results].count(1) == 8
    assert max(r[1] for r in results) <= 700
    # Concurrency-only selection claims the chosen place in the same operation.
    a = one.provider_capacity()
    assert (await a.claim([pool, pool + "#b"], "1:a", 1, 1000))[0] == 0
    assert (await a.claim([pool, pool + "#b"], "1:a", 1, 1000))[0] == 0, "lost reply retry is idempotent"
    assert (await two.provider_capacity().claim([pool, pool + "#b"], "2:b", 1, 1000))[0] == 1


async def test_cancelled_waiter_and_lost_claim_reply_cleanup_exact_owner(provider_kv, monkeypatch):
    one, _, pool = provider_kv
    gate = FairShare()
    held = await gate.acquire(pool, 1, 1)
    waiter = asyncio.create_task(gate.acquire(pool, 2, 1))
    await asyncio.sleep(0.03)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await one._client.zcard(provider_store.pool_keys(pool)[2]) == 0
    await held.aclose()
    original = one._client.eval

    async def lost_reply(script, *args):
        result = await original(script, *args)
        if script == provider_store._CLAIM:
            raise TimeoutError("synthetic lost reply after claim")
        return result

    monkeypatch.setattr(one._client, "eval", lost_reply)
    fallback = await gate.acquire(pool, 2, 1)
    assert not hasattr(fallback, "owned")
    assert gate._pools[pool].active == 1
    assert await one._client.zcard(provider_store.pool_keys(pool)[0]) == 0
    await fallback.aclose()
    assert gate._pools[pool].active == 0
    await gate.aclose()


async def test_cancellation_during_ambiguous_claim_cleans_all_candidate_accounts(provider_kv, monkeypatch):
    one, _, pool = provider_kv
    original, committed = one._client.eval, asyncio.Event()

    async def pause_reply(script, *args):
        result = await original(script, *args)
        if script == provider_store._CLAIM:
            committed.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(one._client, "eval", pause_reply)
    gate = FairShare()
    task = asyncio.create_task(gate.acquire(pool, 1, 1, alternatives=(pool + "#b",)))
    await committed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await one._client.exists(*provider_store.pool_keys(pool)) == 0
    assert await one._client.exists(*provider_store.pool_keys(pool + "#b")) == 0
    assert not gate._pools
    await gate.aclose()


async def test_lost_release_reply_is_idempotent_and_fallback_circuit_breaker_is_bounded(provider_kv, monkeypatch):
    one, _, pool = provider_kv
    original, calls = one._client.eval, []
    gate = FairShare()
    held = await gate.acquire(pool, 1, 1)

    async def lost_reply(script, *args):
        calls.append(script)
        result = await original(script, *args)
        raise TimeoutError("synthetic lost release reply")

    monkeypatch.setattr(one._client, "eval", lost_reply)
    await held.aclose()
    await held.aclose()
    assert len(calls) == 1
    assert await one._client.exists(*provider_store.pool_keys(pool)) == 0
    for _ in range(20):
        local = await gate.acquire(pool, 2, 1)
        await local.aclose()
    assert len(calls) == 1, "fallback during the cooldown sends no spurious cleanup commands"
    assert gate._pools[pool].active == 0
    await gate.aclose()


async def test_failure_fallback_counts_active_calls_and_recovery_does_not_reacquire(provider_kv, monkeypatch):
    one, two, pool = provider_kv
    monkeypatch.setattr(provider_store, "LEASE_MS", 180)
    monkeypatch.setattr(provider_store, "RENEW_S", 0.04)
    gate = FairShare()
    held = await gate.acquire(pool, 1, 1)
    original = one._client.eval

    async def unavailable(*args, **kwargs):
        raise TimeoutError("synthetic outage")

    monkeypatch.setattr(one._client, "eval", unavailable)
    await asyncio.sleep(0.07)
    assert not held.owned
    with pytest.raises(ShareBusy):
        await gate.acquire(pool, 2, 1, max_wait_s=0.03)
    assert gate._pools[pool].active == 1
    # The continuing relay is not cancelled. Another instance can enter after its unrenewed
    # lease expires: this is the documented limitation, not a secretly restored hard cap.
    await asyncio.sleep(0.14)
    assert (await two.provider_capacity().claim([pool], "2:successor", 1, 1000))[1] == "acquired"
    monkeypatch.setattr(one._client, "eval", original)
    one.provider_capacity().unavailable_until = 0
    await held.aclose()
    assert gate._pools[pool].active == 0
    assert await one._client.zrange(provider_store.pool_keys(pool)[0], 0, -1) == ["2:successor"]
    await two.provider_capacity().abandon([pool], "2:successor", 1)
    recovered = await gate.acquire(pool, 3, 1)
    assert recovered.owned and recovered.token != held.token
    await recovered.aclose()
    await gate.aclose()


async def test_lost_queue_state_before_deadline_falls_back_without_releasing_another_owner(provider_kv):
    one, _, pool = provider_kv
    first, second = FairShare(), FairShare()
    held = await first.acquire(pool, 1, 1)
    waiter = asyncio.create_task(second.acquire(pool, 2, 1, max_wait_s=1))
    keys = provider_store.pool_keys(pool)
    async with asyncio.timeout(1):
        while not await one._client.zcard(keys[2]):
            await asyncio.sleep(0.01)
    await one._client.delete(keys[2], keys[3])  # Model partial store loss, not an expired wait.
    fallback = await waiter
    assert not hasattr(fallback, "owned")
    assert await one._client.zrange(keys[0], 0, -1) == [held.token]
    await fallback.aclose()
    assert await one._client.zrange(keys[0], 0, -1) == [held.token]
    await held.aclose()
    await first.aclose()
    await second.aclose()


async def test_waiter_checks_are_batched_and_uncontended_command_cost_is_two(provider_kv, monkeypatch):
    one, _, pool = provider_kv
    calls = []
    original = one._client.eval

    async def counted(script, *args):
        calls.append(script)
        return await original(script, *args)

    monkeypatch.setattr(one._client, "eval", counted)
    gate = FairShare()
    held = await gate.acquire(pool, 1, 1)
    await held.aclose()
    assert calls == [provider_store._CLAIM, provider_store._ABANDON]
    calls.clear()
    held = await gate.acquire(pool, 1, 1)
    waiters = [asyncio.create_task(gate.acquire(pool, n, 1, max_wait_s=0.6)) for n in range(2, 34)]
    await asyncio.sleep(0.56)
    assert calls.count(provider_store._UPDATE) <= 4, "one polling batch per pool, not per waiter"
    for waiter in waiters:
        waiter.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)
    await held.aclose()
    await gate.aclose()


async def test_staggered_calls_share_one_renewal_batch(provider_kv, monkeypatch):
    one, _, pool = provider_kv
    monkeypatch.setattr(provider_store, "LEASE_MS", 600)
    monkeypatch.setattr(provider_store, "RENEW_S", 0.2)
    original, renewals = one.provider_capacity().update, []

    async def counted(*args, **kwargs):
        if kwargs.get("renew"):
            renewals.append(tuple(kwargs["renew"]))
        return await original(*args, **kwargs)

    monkeypatch.setattr(one.provider_capacity(), "update", counted)
    gate, slots = FairShare(), []
    for team in range(8):
        slots.append(await gate.acquire(pool, team, 8))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.18)
    assert len(renewals) == 1 and set(renewals[0]) == {s.token for s in slots}
    assert all(s.owned for s in slots)
    for slot in slots:
        await slot.aclose()
    await gate.aclose()


async def test_slow_store_has_bounded_failure_and_local_fallback(provider_kv, monkeypatch):
    one, _, pool = provider_kv
    calls = []

    async def stalled(*args):
        calls.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(one._client, "eval", stalled)
    gate = FairShare()
    started = time.monotonic()
    slot = await gate.acquire(pool, 1, 1)
    assert time.monotonic() - started < 1.0, "provider admission keeps the short optional-work budget"
    assert len(calls) == 2, "one ambiguous claim and one cleanup, never an unbounded retry"
    assert not hasattr(slot, "owned")
    await slot.aclose()
    await gate.aclose()


async def test_shared_call_path_preserves_busy_byok_pins_and_no_db_during_admission(
    clients, platform_on, provider_kv, monkeypatch,
):
    from treg.application.call import keyslots
    from treg.domain.capacity.view import view as capacity_view
    from treg.infra.db import _engine

    one, _, pool = provider_kv
    monkeypatch.setattr(keyslots, "capacity_key", lambda provider, slot: pool)
    monkeypatch.setattr(capacity_view, "concurrency", lambda provider: 1)
    get_settings().fair_share_wait_s = 0.04
    monkeypatch.setattr(service, "relay", _fake_relay(200, b'{"ok":true}'))
    # Warm resolution separately; the following observation is specifically the admission phase.
    assert (await clients.get(f"/call/{EP}?aweme_id=warm")).status_code == 200
    seen, claim = [], one.provider_capacity().claim

    async def checked_claim(*args, **kwargs):
        seen.append(_engine.pool.checkedout())
        return await claim(*args, **kwargs)

    monkeypatch.setattr(one.provider_capacity(), "claim", checked_claim)
    before = await _balance(clients)
    blocker = await fair_share.acquire(pool, -1, 1)
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "synthetic-pin"})
    assert r.status_code == 429 and r.json()["detail"]["error"] == "share_busy", r.text
    assert await _balance(clients) == before
    await blocker.aclose()
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "synthetic-pin"})
    assert r.status_code == 200
    assert seen and set(seen) == {0}
    assert await one._client.exists(*provider_store.pool_keys(pool)) == 0
    await clients.post("/secrets", json={"name": "tikhub", "value": "SYNTHETIC-BYOK"})
    blocker = await fair_share.acquire(pool, -1, 1)
    count = len(seen)
    assert (await clients.get(f"/call/{EP}?aweme_id=8")).status_code == 200
    assert len(seen) == count, "BYOK never consults platform admission"
    await blocker.aclose()


@pytest.mark.parametrize("exit_path", ["complete", "cancel", "error", "close_only"])
async def test_stream_holds_slot_until_closed_and_closes_once(provider_kv, exit_path):
    one, _, pool = provider_kv
    gate = FairShare()
    held = await gate.acquire(pool, 1, 1)
    entered, resume = asyncio.Event(), asyncio.Event()
    closed = []

    async def stream():
        entered.set()
        yield b"first"
        await resume.wait()
        if exit_path == "error":
            raise OSError("synthetic upstream stream error")
        yield b"last"

    async def close():
        closed.append(True)

    response = UpstreamResponse(200, (), stream(), close)
    service._share_until_closed(response, held)

    async def consume():
        return b"".join([chunk async for chunk in response.body_stream])

    if exit_path != "close_only":
        task = asyncio.create_task(consume())
        await entered.wait()
        assert await one._client.zcard(provider_store.pool_keys(pool)[0]) == 1
        with pytest.raises(ShareBusy):
            await gate.acquire(pool, 2, 1, max_wait_s=0.02)
        if exit_path == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            resume.set()
            if exit_path == "error":
                with pytest.raises(OSError):
                    await task
            else:
                assert await task == b"firstlast"
    await response.close()
    await response.close()
    assert closed == [True]
    assert gate._pools[pool].active == 0
    assert await one._client.exists(*provider_store.pool_keys(pool)) == 0
    await gate.aclose()


@pytest.mark.parametrize("lease_loss", [False, True])
async def test_real_http_stream_retains_admission_and_survives_lease_loss(
    wire, provider_kv, monkeypatch, lease_loss,
):
    from treg.application.call import keyslots
    from treg.domain.capacity.view import view as capacity_view

    client, state = wire
    one, _, pool = provider_kv
    monkeypatch.setattr(keyslots, "capacity_key", lambda provider, slot: pool)
    monkeypatch.setattr(capacity_view, "concurrency", lambda provider: 1)
    get_settings().fair_share_wait_s = 0.04
    if lease_loss:
        monkeypatch.setattr(provider_store, "LEASE_MS", 180)
        monkeypatch.setattr(provider_store, "RENEW_S", 0.04)
    state["pause"] = Event()
    async with client.stream("GET", f"/call/{FETCH}?id=synthetic-task") as response:
        assert response.status_code == 200
        stream = response.aiter_bytes()
        first = await anext(stream)
        assert await one._client.zcard(provider_store.pool_keys(pool)[0]) == 1
        busy = await client.get(f"/call/{FETCH}?id=synthetic-task")
        assert busy.status_code == 429 and "share_busy" in busy.text
        if lease_loss:
            original = one._client.eval

            async def fail_renew(script, *args):
                if script == provider_store._UPDATE:
                    raise TimeoutError("synthetic renewal failure")
                return await original(script, *args)

            monkeypatch.setattr(one._client, "eval", fail_renew)
            await asyncio.sleep(0.25)
            state["pause"].set()
            body = first + b"".join([chunk async for chunk in stream])
            assert body == state["body"], "lease loss never terminates a valid stream"
        # Otherwise disconnect with upstream still blocked; its socket and lease must close.
    async with asyncio.timeout(2):
        while await one._client.zcard(provider_store.pool_keys(pool)[0]):
            await asyncio.sleep(0.01)
    assert fair_share._pools[pool].active == 0


@pytest.mark.parametrize("fault", [asyncio.CancelledError, RuntimeError])
async def test_session_cleanup_failure_cannot_orphan_a_stream_lease(provider_kv, monkeypatch, fault):
    one, _, pool = provider_kv
    gate = FairShare()
    slot = await gate.acquire(pool, 1, 1)
    closed = []

    async def body():
        yield b"bytes"

    async def close():
        closed.append(True)

    async def db_close():
        raise fault("synthetic session close failure")

    response = UpstreamResponse(200, (), body(), close)
    request = SimpleNamespace(
        share_slot=slot, lease=None, db=SimpleNamespace(close=db_close),
        state=SimpleNamespace(idem_claim=None, call_audited=False, call_cost_micro=0))

    async def execute(*args):
        return response

    monkeypatch.setattr(service, "_ApplicationRequest", lambda context: request)
    monkeypatch.setattr(service, "_execute_call", execute)
    with pytest.raises(fault):
        await service.execute_call(SimpleNamespace(), None)
    assert closed == [True] and gate._pools[pool].active == 0
    assert await one._client.exists(*provider_store.pool_keys(pool)) == 0
    await gate.aclose()


def _process_calls(url, pool, barrier, output, mode):
    """Spawn imports fresh state: no inherited gate, rate bucket, client, or event loop."""
    os.environ["TREG_KV_URL"] = url
    get_settings.cache_clear()

    async def run():
        gate, limiter = FairShare(), Limiter()
        await asyncio.to_thread(barrier.wait, 10)
        events = []
        for _ in range(3):
            if mode == "concurrency":
                slot = await gate.acquire(pool, os.getpid(), 2)
                events.append((time.monotonic(), 1))
                await asyncio.sleep(0.1)
                events.append((time.monotonic(), -1))
                await slot.aclose()
            else:
                await limiter.acquire(pool, 12, 1)
                events.append((time.monotonic(), 1))
        await gate.aclose()
        await kv.close()
        output.put(events)

    asyncio.run(run())


def _crashed_process(url, pool, pipe):
    async def run():
        store = kv.RedisStore(url)
        result = await store.provider_capacity().claim([pool], "1:crashed", 1, 1000, lease_ms=200)
        pipe.send(result)
        os._exit(0)  # No cleanup/close: the owner disappears with a live lease.
    asyncio.run(run())


async def test_process_crash_recovers_at_expiry(provider_kv):
    one, _, pool = provider_kv
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_crashed_process, args=(os.environ["TREG_TEST_KV_URL"], pool, send))
    child.start()
    try:
        assert await asyncio.to_thread(receive.poll, 10)
        assert receive.recv()[1] == "acquired"
        assert (await one.provider_capacity().claim([pool], "2:survivor", 1, 1000))[1] == "waiting"
        await asyncio.sleep(0.22)
        state = await one.provider_capacity().update(pool, 1, waiting=["2:survivor"])
        assert state["2:survivor"] == ("acquired", 1)
        await one.provider_capacity().abandon([pool], "1:crashed", 1)
        assert await one._client.zrange(provider_store.pool_keys(pool)[0], 0, -1) == ["2:survivor"]
    finally:
        await asyncio.to_thread(child.join, 5)
        if child.is_alive():
            child.terminate()
            child.join(5)
        receive.close()
        send.close()


@pytest.mark.parametrize("mode", ["concurrency", "rate"])
async def test_spawned_processes_share_one_allowance(provider_kv, mode):
    _, _, pool = provider_kv
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(4), ctx.Queue()
    children = [ctx.Process(target=_process_calls, args=(os.environ["TREG_TEST_KV_URL"], pool,
                                                        barrier, output, mode)) for _ in range(4)]
    try:
        for child in children:
            child.start()
        groups = [await asyncio.to_thread(output.get, True, 20) for _ in children]
        for child in children:
            await asyncio.to_thread(child.join, 5)
            assert child.exitcode == 0
        events = sorted(event for group in groups for event in group)
        if mode == "concurrency":
            active, peak = 0, 0
            for _, delta in events:
                active += delta
                peak = max(peak, active)
            assert active == 0 and peak == 2, "cap is total, never processes times cap"
        else:
            stamps = [stamp for stamp, _ in events]
            assert len(stamps) == 12
            assert stamps[-1] - stamps[0] >= 0.88
            assert min(y - x for x, y in zip(stamps, stamps[1:])) > 0.055
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            await asyncio.to_thread(child.join, 5)
        output.close()
