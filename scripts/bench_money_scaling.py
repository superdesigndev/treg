#!/usr/bin/env python3
"""Synthetic benchmark: does one team's metered-call throughput grow with the process count?

Each call is the money half of a metered call as the call path runs it: reserve under money
admission, then settle under money admission, each in its own money session. No upstream request,
no preflight reads and no audit row are made. Every worker is a separate OS process with its own
event loop, connection pool and Redis client, so cross-process queueing is real. After each run the
ledger is checked: no open hold, one settle entry per call, balance equal to the credit blocks.

Requires TREG_TEST_DB_URL and TREG_TEST_KV_URL on loopback. This drops and recreates the test
schema; the database name must start with treg_admission_test. Loopback has no network latency,
so compare rows with each other, never with a deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

ESTIMATE_MICRO = 100
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DATABASE_PREFIX = "treg_admission_test"
ACTUAL_MICRO = 70


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--procs", default="1,2,4,8", help="process counts to run, comma separated")
    parser.add_argument("--calls", type=int, default=500, help="calls per process")
    parser.add_argument("--concurrency", type=int, default=32, help="in-flight calls per process")
    parser.add_argument("--pool-size", type=int, default=8, help="database connections per process")
    parser.add_argument("--orgs", type=int, default=1, help="teams the calls are spread over")
    parser.add_argument("--admission", default="on,off",
                        help="money admission settings to run, comma separated")
    args = parser.parse_args()
    args.procs = [int(value) for value in args.procs.split(",")]
    args.admission = [value == "on" for value in args.admission.split(",")]
    if min(args.calls, args.concurrency, args.pool_size, args.orgs, *args.procs) < 1:
        parser.error("all numeric arguments must be positive")
    for key in ("TREG_TEST_DB_URL", "TREG_TEST_KV_URL"):
        parsed = urlsplit(os.environ.get(key, ""))
        if parsed.hostname not in LOOPBACK:
            parser.error(f"{key} must explicitly name a disposable loopback service")
        if parsed.query:
            parser.error(f"{key} must not carry query parameters: they can override its target")
    if not urlsplit(os.environ["TREG_TEST_DB_URL"]).path.lstrip("/").startswith(DATABASE_PREFIX):
        parser.error(f"test database name must start with {DATABASE_PREFIX}")
    return args


def _require_disposable(engine) -> None:
    """Refuse unless the DRIVER will connect to the loopback test database.

    The URL's host and path are not what it connects to: query parameters such as `?host=` and
    `?database=` override both, so the check reads the dialect's resolved connection arguments.
    """
    target = engine.dialect.create_connect_args(engine.url)[1]
    database = target.get("database")
    if (target.get("host") not in LOOPBACK or not isinstance(database, str)
            or not database.startswith(DATABASE_PREFIX) or "dsn" in target):
        raise SystemExit("the database connection resolves to something other than the disposable "
                         "loopback test database; refusing to touch it")


def _environment() -> None:
    # A configured secret source outranks environment variables in Settings, so it could point
    # the registry at a database other than the one validated above.
    os.environ.pop("TREG_AWS_SECRET_ID", None)
    os.environ.update(
        TREG_DATABASE_URL=os.environ["TREG_TEST_DB_URL"], TREG_READ_DATABASE_URL="",
        TREG_KV_URL=os.environ["TREG_TEST_KV_URL"], TREG_TELEMETRY="0", TREG_POSTHOG_KEY="",
    )


async def _fund(org_count: int, calls: int) -> list[int]:
    from treg.domain import money as ledger
    from treg.infra import kv
    from treg.infra.db import reset_db, session_maker
    from treg.models import Org

    # The engine the registry built, not the URL asked for: reset_db() below deletes its rows.
    _require_disposable(session_maker.kw["bind"].sync_engine)
    store = kv.RedisStore(os.environ["TREG_TEST_KV_URL"])
    try:
        if not await store.ping():
            raise SystemExit("TREG_TEST_KV_URL does not answer; an admission run would silently "
                             "measure the database path")
    finally:
        await store.aclose()
    await reset_db()
    org_ids = []
    async with session_maker() as db:
        for _ in range(org_count):
            org = Org(id=int(uuid4().hex[:7], 16) + 1, name="scaling-bench", slug=uuid4().hex)
            db.add(org)
            await db.flush()
            await ledger.grant(db, org.id, amount_micro=calls * ESTIMATE_MICRO)
            org_ids.append(org.id)
        await db.commit()
    await session_maker.kw["bind"].dispose()
    return org_ids


async def _verify(org_ids: list[int], calls_per_org: list[int], granted: int) -> None:
    """Every hold closed once, every call billed once, and the balance equal to the blocks."""
    from treg.domain import money as ledger
    from treg.infra.db import session_maker

    async with session_maker() as db:
        for org_id, calls in zip(org_ids, calls_per_org):
            balance = await ledger.balance_of(db, org_id)
            assert not await ledger.open_holds_of(db, org_id), "a hold was left open"
            assert balance == sum(block.remaining_micro for block in await ledger.blocks_of(db, org_id))
            assert balance == granted - calls * ACTUAL_MICRO
            settled = [entry.call_id for entry in await ledger.entries_of(db, org_id, limit=calls * 3)
                       if entry.kind == "settle"]
            assert len(settled) == len(set(settled)) == calls, "a call was billed twice or not at all"
    await session_maker.kw["bind"].dispose()


_ready = None


def _share_barrier(barrier) -> None:
    global _ready
    _ready = barrier


async def _work(admission: bool, org_ids: list[int], calls: int, concurrency: int,
                pool_size: int) -> dict:
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from treg.config import get_settings
    from treg.domain import money as ledger
    from treg.infra import money_admission
    from treg.infra.money_session import money_session

    settings = get_settings()
    settings.platform_margin = 0
    settings.money_admission_enabled = admission
    settings.money_admission_org_ids = []
    settings.money_admission_wait_s = 120
    engine = create_async_engine(os.environ["TREG_TEST_DB_URL"], pool_size=pool_size, max_overflow=0)
    _require_disposable(engine.sync_engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await db.execute(text("SELECT 1"))
    # Loopback hides what a statement costs a deployment inside the lease, so count them: every
    # executed statement plus the transaction's own BEGIN and COMMIT or ROLLBACK, each a round
    # trip on this driver. A statement's first prepare on a connection is one more, not counted.
    statements = 0

    def count(*_args) -> None:
        nonlocal statements
        statements += 1

    for name in ("before_cursor_execute", "begin", "commit", "rollback"):
        event.listen(engine.sync_engine, name, count)
    slots = asyncio.Semaphore(concurrency)
    latencies: list[float] = []

    async def call(index: int) -> None:
        org_id = org_ids[index % len(org_ids)]
        call_id = uuid4().hex
        async with slots:
            started = time.perf_counter()
            async with (money_admission.admit([org_id], operation="reserve"),
                        money_session(sessions()) as db):
                await ledger.reserve_in_transaction(
                    db, org_id, "bench.scaling", ESTIMATE_MICRO, call_id=call_id)
                await db.commit()
            await asyncio.sleep(0)  # the upstream request would run here, holding no connection
            async with (money_admission.admit([org_id], operation="close"),
                        money_session(sessions()) as db):
                await ledger.settle_in_transaction(db, call_id, ACTUAL_MICRO)
                await db.commit()
            latencies.append((time.perf_counter() - started) * 1000)

    # Imports, the pool and the first connection are done: wait for every other worker to get
    # here, so no worker runs alone while the rest are still starting.
    await asyncio.to_thread(_ready.wait, 120)
    started_at = time.time()
    await asyncio.gather(*(call(index) for index in range(calls)))
    ended_at = time.time()
    await engine.dispose()
    # A run that fell back to the database path, or lost a lease, did not measure the shared lease.
    unleased = sum(
        row["lease_lost"] + sum(value for key, value in row.items() if key.startswith("fallback_"))
        + (row["completed"] if row["mode"] != "redis" else 0)
        for row in money_admission.snapshot()) if admission else 0
    return {"started_at": started_at, "ended_at": ended_at, "statements": statements,
            "unleased": unleased,
            "latencies_ms": latencies}


def _process(arguments: tuple) -> dict:
    _environment()
    return asyncio.run(_work(*arguments))


def main() -> None:
    args = _arguments()
    _environment()
    spawn = multiprocessing.get_context("spawn")
    rows = []
    for admission in args.admission:
        for procs in args.procs:
            total = procs * args.calls
            org_ids = asyncio.run(_fund(args.orgs, total))
            with spawn.Pool(procs, initializer=_share_barrier,
                            initargs=(spawn.Barrier(procs),)) as pool:
                results = pool.map(_process, [(admission, org_ids, args.calls, args.concurrency,
                                               args.pool_size)] * procs, chunksize=1)
            # The interval that covers every worker, not the longest single worker.
            elapsed = (max(result["ended_at"] for result in results)
                       - min(result["started_at"] for result in results))
            per_org = [procs * len(range(k, args.calls, args.orgs)) for k in range(args.orgs)]
            asyncio.run(_verify(org_ids, per_org, total * ESTIMATE_MICRO))
            # Percentiles of every call together: a mean or a maximum of per-worker percentiles
            # is not a percentile of the run.
            latencies = sorted(ms for result in results for ms in result["latencies_ms"])
            unleased = sum(result["unleased"] for result in results)
            if unleased:
                raise SystemExit(f"{unleased} money sessions ran without the Redis lease "
                                 "(fallback or lease loss); this admission run is not a measurement")
            row = {
                "admission": admission, "orgs": args.orgs, "procs": procs, "calls": total,
                "concurrency_per_proc": args.concurrency,
                "throughput_per_s": round(total / elapsed, 1),
                "db_statements_per_call": round(
                    sum(result["statements"] for result in results) / total, 2),
                "latency_p50_ms": round(latencies[len(latencies) // 2], 2),
                "latency_p95_ms": round(latencies[int((len(latencies) - 1) * .95)], 2),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
    print(json.dumps({"synthetic_only": True, "client_model": "one OS process per worker",
                      "rows": rows}), flush=True)


if __name__ == "__main__":
    main()
