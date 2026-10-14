"""Admission isolation against real loopback PostgreSQL and Redis, never production.

Opt in with TREG_TEST_DB_URL and TREG_TEST_KV_URL naming disposable local instances.
The database remains the accounting authority when admission cannot serialize callers.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import nullcontext
from contextvars import ContextVar
from uuid import uuid4
from urllib.parse import urlsplit

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra import kv
from treg.infra.db import reset_db, session_maker
from treg.infra.money_session import money_session
from treg.models import CreditBlock, Org


@pytest.fixture
async def local_postgres(monkeypatch):
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires disposable PostgreSQL via TREG_TEST_DB_URL")
    assert engine.url.host in {"127.0.0.1", "localhost", "::1"}, "local test DB only"
    await reset_db()
    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    return engine


@pytest.fixture
async def local_redis(monkeypatch):
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    stores = [kv.RedisStore(url), kv.RedisStore(url)]
    assert all([await store.ping() for store in stores])
    monkeypatch.setattr(get_settings(), "kv_url", url)
    monkeypatch.setattr(kv, "store", lambda: stores[0])
    yield stores
    for store in stores:
        await store.aclose()


async def _funded_holds(slug: str, count: int) -> tuple[int, list[str]]:
    async with session_maker() as db:
        # Distinct identities also isolate Redis keys across parallel test databases.
        org = Org(id=int(uuid4().hex[:7], 16) + 1, name=slug, slug=f"{slug}-{uuid4().hex}")
        db.add(org)
        await db.flush()
        assert org.id is not None
        await ledger.grant(db, org.id, amount_micro=10_000)
        await db.commit()
        calls = [await ledger.reserve(db, org.id, "test.admission", 100,
                                      call_id=uuid4().hex) for _ in range(count)]
        return org.id, calls


async def _accounting(org_id: int, calls: list[str], charge: int = 70) -> None:
    async with session_maker() as db:
        blocks = await ledger.blocks_of(db, org_id)
        holds = await ledger.open_holds_of(db, org_id)
        balance = await ledger.balance_of(db, org_id)
        assert balance == sum(b.remaining_micro for b in blocks) - sum(h.amount_micro for h in holds)
        assert holds == []
        assert balance == 10_000 - len(calls) * charge
        entries = await ledger.entries_of(db, org_id, limit=10_000)
        settled = [entry for entry in entries if entry.kind == "settle"]
        assert sorted(entry.call_id for entry in settled) == sorted(calls)
        assert all(entry.amount_micro == -charge for entry in settled)


async def _wait_until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def _close(sessions, org_id: int, call_id: str, *, admission=None) -> int:
    gate = admission([org_id], operation="close") if admission else nullcontext()
    async with gate:
        async with money_session(sessions()) as db:
            result = await ledger.settle_in_transaction(db, call_id, 70)
            await db.commit()
            return result


async def _hot_org_isolation(engine, *, admission=None, isolates: bool | None = None) -> dict:
    isolates = bool(admission) if isolates is None else isolates
    hot, calls = await _funded_holds("admission-hot", 6)
    other, [other_call] = await _funded_holds("admission-other", 1)
    pooled = create_async_engine(engine.url, pool_size=3, max_overflow=0, pool_timeout=5)
    sessions = async_sessionmaker(pooled, expire_on_commit=False)
    pids: set[int] = set()
    tasks: list[asyncio.Task] = []
    peak = 0

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        nonlocal peak
        peak = max(peak, pooled.pool.checkedout())
        task = asyncio.current_task()
        if task and task.get_name().startswith("admission-hot:") and "FOR UPDATE" in statement.upper():
            pids.add(conn.connection.driver_connection.get_server_pid())

    event.listen(pooled.sync_engine, "before_cursor_execute", before_sql)
    try:
        async with asyncio.timeout(12):
            async with session_maker() as holder:
                await holder.execute(select(CreditBlock.id).where(CreditBlock.org_id == hot).with_for_update())
                holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                tasks = [asyncio.create_task(_close(sessions, hot, call, admission=admission),
                                             name=f"admission-hot:{i}") for i, call in enumerate(calls)]
                expected = 1 if isolates else 3
                await _wait_until(lambda: len(pids) == expected)
                # Confirm a real PostgreSQL lock wait, not merely a scheduled-but-unstarted task.
                async with session_maker() as observer:
                    async with asyncio.timeout(5):
                        while True:
                            waiting = [(await observer.execute(text("SELECT pg_blocking_pids(:pid)"),
                                        {"pid": pid})).scalar_one() for pid in pids]
                            if any(holder_pid in blockers for blockers in waiting):
                                break
                            await asyncio.sleep(0.005)
                blocked_occupancy = pooled.pool.checkedout()
                assert blocked_occupancy == expected
                other_task = asyncio.create_task(_close(sessions, other, other_call, admission=admission))
                tasks.append(other_task)
                completed, _ = await asyncio.wait({other_task}, timeout=0.25)
                if isolates:
                    assert completed and await other_task == 70, "unrelated org must retain DB capacity"
                    assert len(pids) == 1, "same-org waiters must remain outside the DB pool"
                else:
                    assert not completed, "baseline must demonstrate pool starvation"
                await holder.rollback()
                assert await asyncio.gather(*tasks) == [70] * len(tasks)
        await _accounting(hot, calls)
        await _accounting(other, [other_call])
        return {"blocked_pool_occupancy": blocked_occupancy, "peak_pool_occupancy": peak}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(pooled.sync_engine, "before_cursor_execute", before_sql)
        await pooled.dispose()


async def test_hot_org_without_admission_exhausts_shared_pool(local_postgres):
    assert (await _hot_org_isolation(local_postgres))["blocked_pool_occupancy"] == 3


async def test_hot_org_admission_leaves_capacity_for_another_org(local_postgres, local_redis, monkeypatch):
    from treg.infra import money_admission

    monkeypatch.setattr(get_settings(), "money_admission_enabled", True)
    monkeypatch.setattr(get_settings(), "money_admission_org_ids", [])
    monkeypatch.setattr(get_settings(), "money_admission_wait_s", 5)
    monkeypatch.setattr(get_settings(), "money_admission_lease_s", 15)
    assert (await _hot_org_isolation(local_postgres, admission=money_admission.admit))[
        "blocked_pool_occupancy"] == 1


async def test_admission_timeout_keeps_accounting_safe_but_loses_pool_isolation(
    local_postgres, local_redis, monkeypatch,
):
    from treg.infra import money_admission

    monkeypatch.setattr(get_settings(), "money_admission_enabled", True)
    monkeypatch.setattr(get_settings(), "money_admission_org_ids", [])
    monkeypatch.setattr(get_settings(), "money_admission_wait_s", 0.05)
    monkeypatch.setattr(get_settings(), "money_admission_lease_s", 15)
    money_admission.snapshot()
    # A long-held row outlasts the admission budget. Accounting still completes exactly once,
    # but fallback callers can fill the old pool: availability fallback is not hard isolation.
    assert (await _hot_org_isolation(local_postgres, admission=money_admission.admit,
                                    isolates=False))["blocked_pool_occupancy"] == 3
    assert sum(row["fallback_wait_timeout"] for row in money_admission.snapshot()) >= 1


@pytest.fixture
def independent_admission_clients(local_redis, monkeypatch):
    """Two real Redis clients with independent local mutexes, as in separate web/worker processes."""
    from treg.infra import money_admission

    selected = ContextVar("admission_test_store", default=local_redis[0])
    monkeypatch.setattr(kv, "store", selected.get)
    monkeypatch.setattr(money_admission, "_local_lock", lambda org_id: asyncio.Lock())
    monkeypatch.setattr(get_settings(), "money_admission_enabled", True)
    monkeypatch.setattr(get_settings(), "money_admission_org_ids", [])
    monkeypatch.setattr(get_settings(), "money_admission_wait_s", 5)
    monkeypatch.setattr(get_settings(), "money_admission_lease_s", 3)
    money_admission.snapshot()

    async def run(store, coroutine):
        token = selected.set(store)
        try:
            return await coroutine
        finally:
            selected.reset(token)

    return money_admission, run


async def test_two_redis_clients_serialize_a_retried_hold(
    local_postgres, local_redis, independent_admission_clients,
):
    admission, run = independent_admission_clients
    org, [call] = await _funded_holds("admission-retry", 1)
    paused, resume, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first():
        async with admission.admit([org], operation="close"):
            async with session_maker() as db:
                amount = await ledger.settle_in_transaction(db, call, 70)
                paused.set()
                await resume.wait()
                await db.commit()
                return amount

    async def second():
        async with admission.admit([org], operation="async"):
            second_entered.set()
            async with session_maker() as db:
                amount = await ledger.settle_in_transaction(db, call, 70)
                await db.commit()
                return amount

    tasks = [asyncio.create_task(run(local_redis[0], first()))]
    try:
        async with asyncio.timeout(8):
            await paused.wait()
            tasks.append(asyncio.create_task(run(local_redis[1], second())))
            await asyncio.sleep(0.15)
            assert not second_entered.is_set(), "cross-client exclusion must come from Redis"
            resume.set()
            assert await asyncio.gather(*tasks) == [70, 0]
        await _accounting(org, [call])
    finally:
        resume.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_expired_lease_does_not_double_charge_or_delete_the_successor(
    local_postgres, local_redis, independent_admission_clients,
):
    admission, run = independent_admission_clients
    org, [call] = await _funded_holds("admission-expiry", 1)
    key = f"money-admission:org:{org}"
    first_ready, resume_first = asyncio.Event(), asyncio.Event()
    second_entered, second_settled, resume_second = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first():
        async with admission.admit([org], operation="close"):
            async with session_maker() as db:
                amount = await ledger.settle_in_transaction(db, call, 70)
                first_ready.set()
                await resume_first.wait()
                await db.commit()
                return amount

    async def second():
        async with admission.admit([org], operation="async"):
            second_entered.set()
            async with session_maker() as db:
                amount = await ledger.settle_in_transaction(db, call, 70)
                second_settled.set()
                await resume_second.wait()
                await db.commit()
                return amount

    tasks = [asyncio.create_task(run(local_redis[0], first()))]
    try:
        async with asyncio.timeout(8):
            await first_ready.wait()
            old_token = await local_redis[1]._client.get(key)
            assert old_token
            # Force real Redis expiry while the old database transaction is still live.
            assert await local_redis[1]._client.pexpire(key, 1)
            while await local_redis[1]._client.exists(key):
                await asyncio.sleep(0.005)
            tasks.append(asyncio.create_task(run(local_redis[1], second())))
            await second_entered.wait()
            successor_token = await local_redis[1]._client.get(key)
            assert successor_token and successor_token != old_token
            assert not second_settled.is_set(), "PostgreSQL must still serialize the duplicate hold"
            resume_first.set()
            assert await tasks[0] == 70
            await second_settled.wait()
            assert await local_redis[1]._client.get(key) == successor_token
            resume_second.set()
            assert await tasks[1] == 0
        await _accounting(org, [call])
    finally:
        resume_first.set()
        resume_second.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await local_redis[0]._client.delete(key)


async def test_redis_unavailable_falls_back_without_duplicate_settlement(
    local_postgres, local_redis, independent_admission_clients,
):
    admission, run = independent_admission_clients
    org, calls = await _funded_holds("admission-offline", 4)
    offline = kv.RedisStore("redis://127.0.0.1:1/0")
    try:
        assert not await offline.ping()
        results = await asyncio.gather(*[
            run(offline, _close(session_maker, org, call, admission=admission.admit))
            for call in calls for _ in range(2)
        ])
        assert sorted(results) == [0] * len(calls) + [70] * len(calls)
        await _accounting(org, calls)
        rows = admission.snapshot()
        assert sum(row["fallback_kv_unavailable"] for row in rows) == 8
    finally:
        await offline.aclose()


async def test_admission_timeout_preserves_foreign_lease_and_settles_once(
    local_postgres, local_redis, independent_admission_clients, monkeypatch,
):
    admission, run = independent_admission_clients
    org, [call] = await _funded_holds("admission-timeout", 1)
    key, token = f"money-admission:org:{org}", uuid4().hex
    monkeypatch.setattr(get_settings(), "money_admission_wait_s", 0.05)
    assert await local_redis[1].acquire_lease(key, token, 5000) == "acquired"
    try:
        results = await asyncio.gather(*[
            run(store, _close(session_maker, org, call, admission=admission.admit))
            for store in local_redis
        ])
        assert sorted(results) == [0, 70]
        assert await local_redis[1]._client.get(key) == token
        await _accounting(org, [call])
        assert sum(row["fallback_wait_timeout"] for row in admission.snapshot()) == 2
    finally:
        await local_redis[1].release_lease(key, token)


@pytest.mark.parametrize("transient_failure", (False, True))
async def test_real_redis_renews_a_lease_until_the_database_scope_exits(
    local_postgres, local_redis, independent_admission_clients, monkeypatch, transient_failure,
):
    from redis.exceptions import TimeoutError as RedisTimeoutError

    admission, run = independent_admission_clients
    original = local_redis[0]._client.eval
    failed = False
    kv.drain_lease_timings()

    async def once_unavailable(script, *args):
        nonlocal failed
        if transient_failure and script == kv._RENEW_LEASE and not failed:
            failed = True
            raise RedisTimeoutError("temporary renewal failure")
        return await original(script, *args)

    monkeypatch.setattr(local_redis[0]._client, "eval", once_unavailable)
    org, [call] = await _funded_holds("admission-renew", 1)
    key = f"money-admission:org:{org}"
    async with admission.admit([org], operation="close"):
        token = await local_redis[1]._client.get(key)
        await asyncio.sleep(3.15)  # Longer than the original real Redis TTL.
        assert await local_redis[1]._client.get(key) == token
        assert await local_redis[1]._client.pttl(key) > 0
        assert await local_redis[1].acquire_lease(key, "other-process", 3000) == "busy"
        assert await _close(session_maker, org, call) == 70
    assert not await local_redis[1]._client.exists(key)
    assert sum(row["lease_lost"] for row in admission.snapshot()) == 0
    assert sum(row["retry_count"] for row in kv.drain_lease_timings()
               if row["phase"] == "renew") == int(transient_failure)
    await _accounting(org, [call])


async def test_cancellation_rolls_back_before_the_other_client_retries(
    local_postgres, local_redis, independent_admission_clients,
):
    admission, run = independent_admission_clients
    org, [call] = await _funded_holds("admission-cancel", 1)
    staged = asyncio.Event()

    async def cancelled_close():
        async with admission.admit([org], operation="close"):
            async with session_maker() as db:
                assert await ledger.settle_in_transaction(db, call, 70) == 70
                staged.set()
                await asyncio.Event().wait()

    first = asyncio.create_task(run(local_redis[0], cancelled_close()))
    second = None
    try:
        async with asyncio.timeout(5):
            await staged.wait()
            second = asyncio.create_task(run(local_redis[1],
                _close(session_maker, org, call, admission=admission.admit)))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert await second == 70
        await _accounting(org, [call])
    finally:
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_two_clients_close_multi_org_batches_with_reversed_input_order(
    local_postgres, local_redis, independent_admission_clients,
):
    admission, run = independent_admission_clients
    first_org, first_calls = await _funded_holds("admission-batch-a", 2)
    second_org, second_calls = await _funded_holds("admission-batch-b", 2)

    async def batch(ids, calls):
        async with admission.admit(ids, operation="deferred"):
            async with session_maker() as db:
                result = await ledger.close_holds_in_transaction(db, [
                    ledger.HoldClose(call, True, 70, "", {}) for call in calls
                ])
                await db.commit()
                return result

    async with asyncio.timeout(5):
        results = await asyncio.gather(
            run(local_redis[0], batch([first_org, second_org], [first_calls[0], second_calls[0]])),
            run(local_redis[1], batch([second_org, first_org], [second_calls[1], first_calls[1]])),
        )
    assert results == [[70, 70], [70, 70]]
    await _accounting(first_org, first_calls)
    await _accounting(second_org, second_calls)
