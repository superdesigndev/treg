#!/usr/bin/env python3
"""Synthetic settlement benchmark on disposable loopback PostgreSQL and Redis.

Requires TREG_TEST_DB_URL and TREG_TEST_KV_URL. This drops/recreates the test schema;
the database name must start with treg_admission_test. It never reads real traffic.
Clients have independent Redis connections and local mutex maps but share one event loop;
this exposes Redis contention without claiming to reproduce process scheduling or network RTT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from contextvars import ContextVar
from urllib.parse import urlsplit
from uuid import uuid4


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--pool-size", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--clients", type=int, default=2,
                        help="independent Redis clients and local mutex maps (one event loop)")
    args = parser.parse_args()
    if min(args.calls, args.concurrency, args.pool_size, args.rounds, args.clients) < 1:
        parser.error("all numeric arguments must be positive")
    for key in ("TREG_TEST_DB_URL", "TREG_TEST_KV_URL"):
        parsed = urlsplit(os.environ.get(key, ""))
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            parser.error(f"{key} must explicitly name a disposable loopback service")
    if not urlsplit(os.environ["TREG_TEST_DB_URL"]).path.lstrip("/").startswith("treg_admission_test"):
        parser.error("test database name must start with treg_admission_test")
    os.environ.update(
        TREG_DATABASE_URL=os.environ["TREG_TEST_DB_URL"], TREG_READ_DATABASE_URL="",
        TREG_KV_URL=os.environ["TREG_TEST_KV_URL"], TREG_TELEMETRY="0", TREG_POSTHOG_KEY="",
    )
    return args


async def _main(args):
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from treg.config import get_settings
    from treg.domain import money as ledger
    from treg.infra import kv, money_admission
    from treg.infra.db import reset_db, session_maker
    from treg.infra.money_session import money_session
    from treg.models import Org

    settings = get_settings()
    settings.platform_margin = 0
    settings.money_admission_org_ids = []
    settings.money_admission_wait_s = 5
    stores = [kv.RedisStore(os.environ["TREG_TEST_KV_URL"]) for _ in range(args.clients)]
    assert all([await store.ping() for store in stores])
    selected = ContextVar("benchmark_client", default=0)
    original_store, original_lock = kv.store, money_admission._local_lock
    kv.store = lambda: stores[selected.get()]
    # Separate processes have separate local mutex maps. Keep that property here so Redis
    # contention is measured instead of being hidden by a shared asyncio.Lock.
    local_locks = [{} for _ in stores]

    def local_lock(org_id):
        return local_locks[selected.get()].setdefault(org_id, asyncio.Lock())

    money_admission._local_lock = local_lock
    rows = []
    try:
        for round_id in range(args.rounds):
            for scenario, org_count, concurrency in (
                ("uncontended", 1, 1),
                ("same_org", 1, args.concurrency),
                ("multiple_orgs", 8, args.concurrency),
            ):
                for enabled in ((False, True) if round_id % 2 == 0 else (True, False)):
                    await reset_db()
                    settings.money_admission_enabled = enabled
                    work = []
                    orgs = []
                    async with session_maker() as db:
                        for i in range(org_count):
                            org = Org(id=int(uuid4().hex[:7], 16) + 1,
                                      name="admission-bench", slug=uuid4().hex)
                            db.add(org)
                            await db.flush()
                            await ledger.grant(db, org.id, amount_micro=args.calls * 100)
                            orgs.append(org.id)
                        await db.commit()
                        for i in range(args.calls):
                            org_id = orgs[i % len(orgs)]
                            call_id = await ledger.reserve(db, org_id, "test.admission", 100,
                                                           call_id=uuid4().hex)
                            work.append((org_id, call_id))
                    engine = create_async_engine(os.environ["TREG_TEST_DB_URL"],
                                                 pool_size=args.pool_size, max_overflow=0)
                    sessions = async_sessionmaker(engine, expire_on_commit=False)
                    # Warm the full pool symmetrically, so connection establishment does not
                    # distort the off/on comparison. All actual settlement work remains timed.
                    ready = asyncio.Barrier(args.pool_size)

                    async def warm():
                        async with sessions() as db:
                            await db.execute(text("SELECT 1"))
                            await ready.wait()

                    await asyncio.gather(*(warm() for _ in range(args.pool_size)))
                    peak = 0
                    connection_ms = 0.0

                    def checkout(connection, record, proxy):
                        nonlocal peak
                        peak = max(peak, engine.pool.checkedout())
                        record.info["benchmark_checked_out"] = time.perf_counter()

                    def checkin(connection, record):
                        nonlocal connection_ms
                        started = record.info.pop("benchmark_checked_out", None)
                        if started is not None:
                            connection_ms += (time.perf_counter() - started) * 1000

                    event.listen(engine.sync_engine, "checkout", checkout)
                    event.listen(engine.sync_engine, "checkin", checkin)
                    semaphore = asyncio.Semaphore(concurrency)
                    latencies = []
                    money_admission.snapshot()

                    async def settle(index, org_id, call_id):
                        async with semaphore:
                            token = selected.set((index // org_count) % len(stores))
                            start = time.perf_counter()
                            try:
                                async with money_admission.admit([org_id], operation="close"):
                                    async with money_session(sessions()) as db:
                                        assert await ledger.settle_in_transaction(db, call_id, 70) == 70
                                        await db.commit()
                            finally:
                                selected.reset(token)
                            latencies.append((time.perf_counter() - start) * 1000)

                    try:
                        start = time.perf_counter()
                        await asyncio.gather(*[settle(i, *item) for i, item in enumerate(work)])
                        elapsed = time.perf_counter() - start
                        metrics = money_admission.snapshot()
                        settlement_connection_ms = connection_ms
                        async with sessions() as db:
                            for org in orgs:
                                count = sum(org_id == org for org_id, _ in work)
                                balance = await ledger.balance_of(db, org)
                                blocks = await ledger.blocks_of(db, org)
                                holds = await ledger.open_holds_of(db, org)
                                assert not holds
                                assert balance == sum(b.remaining_micro for b in blocks)
                                assert balance == args.calls * 100 - count * 70
                                entries = await ledger.entries_of(db, org, limit=args.calls * 3)
                                settled = [e for e in entries if e.kind == "settle"]
                                assert len(settled) == count
                                assert len({e.call_id for e in settled}) == count
                        row = {
                            "round": round_id, "scenario": scenario, "orgs": org_count, "enabled": enabled,
                            "calls": args.calls, "concurrency": concurrency, "clients": args.clients,
                            "wall_ms": round(elapsed * 1000, 3),
                            "throughput_per_s": round(args.calls / elapsed, 3),
                            "latency_mean_ms": round(statistics.mean(latencies), 3),
                            "latency_p95_ms": round(sorted(latencies)[int((len(latencies) - 1) * .95)], 3),
                            "peak_db_occupancy": peak,
                            "connection_ms_per_call": round(settlement_connection_ms / args.calls, 3),
                            "admission_wait_mean_ms": round(sum(m["wait_total_ms"] for m in metrics) / args.calls, 3),
                            "fallbacks": sum(sum(v for k, v in m.items() if k.startswith("fallback_"))
                                             for m in metrics),
                        }
                        rows.append(row)
                        print(json.dumps(row), flush=True)
                    finally:
                        event.remove(engine.sync_engine, "checkout", checkout)
                        event.remove(engine.sync_engine, "checkin", checkin)
                        await engine.dispose()
        print(json.dumps({"synthetic_only": True,
                          "client_model": "independent mutex maps in one event loop",
                          "rounds": rows}), flush=True)
    finally:
        kv.store = original_store
        money_admission._local_lock = original_lock
        for store in stores:
            await store.aclose()
        await session_maker.kw["bind"].dispose()


if __name__ == "__main__":
    asyncio.run(_main(_arguments()))
