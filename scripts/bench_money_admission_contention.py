#!/usr/bin/env python3
"""Compare admission policies in spawned Python processes against a private local Redis.

Runs synthetic held scopes, not HTTP, PostgreSQL or real accounting. Starts its own Redis
on an ephemeral loopback port, disables persistence, checks its PID and stops it on exit.
No credentials, service URLs or workloads are read from the environment. Example:

  PYTHONPATH=src python scripts/bench_money_admission_contention.py \
      --baseline-ref <commit-with-local-gate> --output /tmp/admission.json

The baseline admission and KV modules are loaded from Git; candidates retain the working tree's local org gate and
replace only its busy delay expression. "full" has a 3 ms floor; "equal" uses U(ceiling/2, ceiling);
"decorrelated" uses min(cap, U(3 ms, previous*3)). All preserve the original wait budget.
"current" runs the working-tree implementation unchanged. Fairness is measured, not promised.
"--pressure" adds bounded Lua CPU work to this disposable server; it is not a CPU quota.
"--balanced-order" balances each policy's execution position across complete rotation blocks.
"--lease-rtt-ms" injects cancellable delay around actual Redis SET/EVAL commands, inside the
existing KV deadlines. It is synthetic application latency, not a network or CPU-quota model.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import multiprocessing as mp
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from types import ModuleType, SimpleNamespace
from uuid import uuid4


SCENARIOS = {
    "hot_short": (1, 0.002, 0),
    "hot_mixed": (1, 0.002, 0.02),
    "many_short": (8, 0.002, 0),
    "many_mixed": (8, 0.002, 0.02),
}
DELAY = "await asyncio.sleep(random.uniform(retry_s / 2, retry_s))"
GROW = "retry_s = min(retry_s * 2, _BUSY_RETRY_CAP_S)"


def percentile(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int((len(values) - 1) * q))], 3) if values else 0


def load_policy(source, variant):
    if variant == "fixed":
        source = source.replace(DELAY, "await asyncio.sleep(random.uniform(0.003, 0.010))")
    elif variant.startswith("full"):
        source = source.replace(DELAY, "await asyncio.sleep(max(0.003, random.uniform(0, retry_s)))")
    elif variant.startswith("decorrelated"):
        source = source.replace("retry_s = _BUSY_RETRY_INITIAL_S", "retry_s = 0.003")
        source = source.replace(DELAY, "retry_s = min(_BUSY_RETRY_CAP_S, random.uniform(0.003, retry_s * 3))\n"
                                "                    await asyncio.sleep(retry_s)").replace(GROW, "pass")
    module = ModuleType("treg.infra._contention_benchmark")
    sys.modules[module.__name__] = module
    exec(compile(source, "<benchmark-admission>", "exec"), module.__dict__)
    if variant not in {"local_fixed", "fixed", "current"}:
        module._BUSY_RETRY_CAP_S = int(variant.split(":")[1]) / 1000
    return module


def load_kv(source):
    # Each spawned process runs one policy. Installing its matching KV module before
    # admission is imported keeps the Git baseline's lease timeout/retry contracts intact.
    import treg.infra

    module = ModuleType("treg.infra.kv")
    sys.modules[module.__name__] = module
    treg.infra.kv = module
    exec(compile(source, "<benchmark-kv>", "exec"), module.__dict__)
    return module


def policy_order(variants, repeat, scenario_index, balanced):
    order = list(variants)
    if balanced:
        random.Random(1729 + scenario_index).shuffle(order)
        # For two policies and four rounds: AB, BA, BA, AB (or its reverse).
        # Each complete block places every policy in every execution position.
        if (repeat // len(order)) % 2:
            order.reverse()
        offset = repeat % len(order)
        return order[offset:] + order[:offset]
    random.Random(1729 + repeat).shuffle(order)
    return order


def jain(values):
    return round(sum(values)**2 / (len(values) * sum(n*n for n in values)), 4) if any(values) else 0


async def run_worker(config, index, barrier, ready):
    kv = load_kv(config["kv_source"])
    policy = load_policy(config["source"], config["variant"])
    random.seed(config["jitter_seed"] + index * 1000)
    policy.get_settings = lambda: SimpleNamespace(
        money_admission_enabled=True, money_admission_org_ids=[],
        money_admission_wait_s=5, money_admission_lease_s=15)
    store = kv.RedisStore(config["url"])
    kv.configured, kv.store = lambda: True, lambda: store
    org_count, short_s, long_probability = SCENARIOS[config["scenario"]]
    # Each scenario has its own synthetic org identities, shared by all processes.
    org_base = config["org_base"]
    concurrency = config["concurrency"]
    await asyncio.gather(*(store.ping() for _ in range(concurrency)))
    injected_delay_s = config["lease_rtt_ms"] / 2000
    if injected_delay_s:
        def with_delay(command):
            async def delayed(*args, **kwargs):
                await asyncio.sleep(injected_delay_s)
                result = await command(*args, **kwargs)
                await asyncio.sleep(injected_delay_s)
                return result
            return delayed

        # RedisStore's original timeout contexts still surround each delayed command.
        store._client.set = with_delay(store._client.set)
        store._client.eval = with_delay(store._client.eval)
    barrier.wait(timeout=30)
    if not ready.wait(timeout=30):
        raise RuntimeError("parent did not start the measurement window")
    start = time.monotonic()
    stop = start + config["seconds"]
    cpu_start = time.process_time()
    lags, waits, completed_at = [], [], []
    lane_counts = [0] * concurrency
    lane_total_counts = [0] * concurrency
    lane_max_gaps = [0.0] * concurrency
    holds = Counter()
    holds_total = Counter()
    attempts = Counter()
    original = store.acquire_lease

    async def acquire(*args):
        try:
            result = await original(*args)
        except asyncio.CancelledError:
            attempts["cancelled"] += 1
            raise
        attempts[result] += 1
        return result

    store.acquire_lease = acquire

    async def lag_probe():
        while True:
            target = time.monotonic() + 0.005
            await asyncio.sleep(0.005)
            lags.append(max(0, time.monotonic() - target) * 1000)

    async def lane(lane_id):
        rng = random.Random(config["workload_seed"] + index * 1000 + lane_id)
        serial, last = 0, start
        while time.monotonic() < stop:
            org = org_base + (index * concurrency + lane_id + serial) % org_count
            held_s = 0.2 if rng.random() < long_probability else short_s
            began = time.monotonic()
            async with policy.admit([org], operation="close"):
                entered = time.monotonic()
                waits.append((entered - began) * 1000)
                # Includes fallback entries; Redis acquired counts are separately reported.
                lane_max_gaps[lane_id] = max(lane_max_gaps[lane_id], entered - last)
                last = entered
                await asyncio.sleep(held_s)
            finished = time.monotonic()
            completed_at.append(finished)
            lane_total_counts[lane_id] += 1
            holds_total["long" if held_s == 0.2 else "short"] += 1
            if finished <= stop:
                lane_counts[lane_id] += 1
                holds["long" if held_s == 0.2 else "short"] += 1
            serial += 1
        lane_max_gaps[lane_id] = max(lane_max_gaps[lane_id], max(0, stop - last))

    probe = asyncio.create_task(lag_probe())
    try:
        await asyncio.gather(*(lane(i) for i in range(concurrency)))
    finally:
        probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)
    metrics = policy.snapshot()
    elapsed = time.monotonic() - start
    cpu_s = time.process_time() - cpu_start
    await store.aclose()
    return {
        "pid": __import__("os").getpid(), "worker": index, "cpu_s": cpu_s,
        "elapsed_s": elapsed, "waits_ms": waits, "lags_ms": lags,
        "completions_in_window": sum(lane_counts), "completions_total": len(completed_at),
        "lane_counts": lane_counts, "lane_total_counts": lane_total_counts,
        "lane_max_gaps_s": lane_max_gaps,
        "attempts": dict(attempts), "metrics": metrics, "holds_in_window": dict(holds),
        "holds_total": dict(holds_total),
        "kv_lease_timings": kv.drain_lease_timings() if hasattr(kv, "drain_lease_timings") else [],
    }


def worker(config, index, barrier, ready, queue):
    try:
        queue.put(asyncio.run(run_worker(config, index, barrier, ready)))
    except BaseException:
        queue.put({"worker": index, "error": traceback.format_exc()})


def pressure_worker(url, stop):
    import redis
    client = redis.Redis.from_url(url)
    # Finite arithmetic, no keys or writes. Several short commands share the event loop
    # with lease traffic instead of manufacturing timeout-length stalls.
    script = "local x=0; for i=1,50000 do x=x+i end; return x"
    while not stop.is_set():
        with client.pipeline(transaction=False) as pipe:
            for _ in range(8):
                pipe.eval(script, 0)
            pipe.execute()
        time.sleep(0.01)
    client.close()


@contextmanager
def disposable_redis():
    import redis
    binary = shutil.which("redis-server")
    if not binary:
        raise RuntimeError("redis-server is required")
    with tempfile.TemporaryDirectory(prefix="admission-contention-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with open(Path(directory) / "redis.log", "w+") as log:
            process = subprocess.Popen([binary, "--bind", "127.0.0.1", "--port", str(port),
                                        "--save", "", "--appendonly", "no", "--dir", directory],
                                       stdout=log, stderr=subprocess.STDOUT)
            url = f"redis://127.0.0.1:{port}/0"
            client = redis.Redis.from_url(url, socket_timeout=2)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    try:
                        info = client.info("server")
                        assert info["process_id"] == process.pid, "refuse a pre-existing Redis"
                        break
                    except redis.ConnectionError:
                        time.sleep(0.05)
                else:
                    raise RuntimeError("Redis startup timed out")
                yield url, client, info
            finally:
                client.close()
                process.terminate()
                process.wait(timeout=10)


def run_cell(context, config, count, client, pressure):
    queue, barrier, ready = context.Queue(), context.Barrier(count + 1), context.Event()
    processes = [context.Process(target=worker, args=(config, i, barrier, ready, queue)) for i in range(count)]
    load, stop = None, context.Event()
    if pressure:
        load = context.Process(target=pressure_worker, args=(config["url"], stop))
        load.start()
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=30)
        before = client.info("cpu")
        started = time.monotonic()
        ready.set()
        results = [queue.get(timeout=config["seconds"] + 30) for _ in processes]
        elapsed = time.monotonic() - started
        after = client.info("cpu")
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        errors = [row for row in results if "error" in row]
        if errors:
            raise RuntimeError(errors)
        results.sort(key=lambda row: row["worker"])
        waits = [v for row in results for v in row["waits_ms"]]
        lags = [v for row in results for v in row["lags_ms"]]
        entries = [m for row in results for m in row["metrics"]]
        counts = [row["completions_in_window"] for row in results]
        total_counts = [row["completions_total"] for row in results]
        completed = sum(total_counts)
        lane_counts = [n for row in results for n in row["lane_counts"]]
        redis_counts = [sum(m["completed"] for m in row["metrics"] if m["mode"] == "redis")
                        for row in results]
        attempts = sum(sum(row["attempts"].values()) for row in results)
        worker_cpu_s = sum(row["cpu_s"] for row in results)
        redis_cpu_s = sum(after[k] - before[k] for k in ("used_cpu_user", "used_cpu_sys"))
        summary = {
            "round": config["round"], "scenario": config["scenario"], "variant": config["variant"],
            "processes": count, "concurrency_per_process": config["concurrency"],
            "seconds": config["seconds"], "pressure": pressure,
            "lease_rtt_ms": config["lease_rtt_ms"],
            "execution_index": config["execution_index"], "policy_position": config["policy_position"],
            "workload_seed": config["workload_seed"], "jitter_seed": config["jitter_seed"],
            "completions_in_window": sum(counts), "completions_total": completed,
            "parent_elapsed_s": elapsed,
            "throughput_per_s": round(sum(counts) / config["seconds"], 3),
            "completed_per_s_with_drain": round(completed / elapsed, 3),
            "attempts": attempts, "attempts_per_completion": round(attempts / completed, 6),
            "attempts_per_s": round(attempts / elapsed, 3),
            "worker_cpu_s": worker_cpu_s, "redis_cpu_s": redis_cpu_s,
            "worker_cpu_s_per_1000_completions": round(worker_cpu_s / completed * 1000, 6),
            "redis_cpu_s_per_1000_completions": round(redis_cpu_s / completed * 1000, 6),
            "worker_cpu_pct_one_core": round(worker_cpu_s / elapsed * 100, 3),
            "redis_cpu_pct_one_core": round(redis_cpu_s / elapsed * 100, 3),
            "wait_p50_ms": percentile(waits, .5), "wait_p95_ms": percentile(waits, .95),
            "wait_p99_ms": percentile(waits, .99), "wait_max_ms": round(max(waits), 3),
            "lag_p95_ms": percentile(lags, .95), "lag_p99_ms": percentile(lags, .99),
            "process_completions": counts,
            "process_completions_total": total_counts,
            "long_holds_in_window": sum(row["holds_in_window"].get("long", 0) for row in results),
            "long_holds_total": sum(row["holds_total"].get("long", 0) for row in results),
            "process_redis_completions": redis_counts,
            "process_jain": jain(counts), "process_redis_jain": jain(redis_counts),
            "lane_jain": jain(lane_counts),
            "zero_completion_lanes": sum(n == 0 for row in results for n in row["lane_counts"]),
            "longest_lane_gap_s": round(max(v for row in results for v in row["lane_max_gaps_s"]), 3),
            "wait_over_1s": sum(v > 1000 for v in waits),
            "fallbacks": sum(v for m in entries for k, v in m.items() if k.startswith("fallback_")),
            "wait_timeouts": sum(m["fallback_wait_timeout"] for m in entries),
            "wait_timeout_fraction": sum(m["fallback_wait_timeout"] for m in entries) / completed,
            "kv_errors": sum(m["kv_errors"] for m in entries),
            "remaining_waiters": sum(m["waiting"] for m in entries),
            "remaining_active": sum(m["active"] for m in entries),
            "lease_lost": sum(m["lease_lost"] for m in entries),
        }
        assert not client.exists(*(f"money-admission:org:{config['org_base'] + i}"
                                   for i in range(SCENARIOS[config["scenario"]][0])))
        return {"summary": summary, "workers": results,
                "parent_elapsed_s": elapsed, "redis_cpu_before": before, "redis_cpu_after": after}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        stop.set()
        if load:
            load.join(timeout=5)
            if load.is_alive():
                load.terminate()
                load.join(timeout=5)
        queue.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=6)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--variants", nargs="+", default=[
        "local_fixed", "full:12", "full:24", "equal:12", "equal:24", "equal:48", "decorrelated:24"])
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--pressure", action="store_true")
    parser.add_argument("--balanced-order", action="store_true",
                        help="balance policy positions; rounds must be a multiple of the number of variants")
    parser.add_argument("--lease-rtt-ms", type=float, default=0,
                        help="synthetic total delay per Redis lease SET/EVAL command; not real network latency")
    args = parser.parse_args()
    if min(args.seconds, args.processes, args.concurrency, args.rounds) <= 0:
        parser.error("numeric arguments must be positive")
    if not math.isfinite(args.seconds) or not math.isfinite(args.lease_rtt_ms) or args.lease_rtt_ms < 0:
        parser.error("seconds must be finite and lease-rtt-ms must be finite and nonnegative")
    if len(set(args.variants)) != len(args.variants):
        parser.error("variants must be unique")
    if args.balanced_order and args.rounds % len(args.variants):
        parser.error("balanced-order requires rounds to be a multiple of the number of variants")
    valid = {"current", "fixed", "local_fixed"} | {
        f"{name}:{cap}" for name in ("equal", "full", "decorrelated") for cap in (6, 12, 24, 48, 96, 192)}
    if any(name not in valid for name in args.variants):
        parser.error("unknown variant")
    repo = Path(__file__).resolve().parents[1]
    current = (repo / "src/treg/infra/money_admission.py").read_text()
    current_kv = (repo / "src/treg/infra/kv.py").read_text()
    baseline_commit = subprocess.check_output(["git", "rev-parse", f"{args.baseline_ref}^{{commit}}"],
                                             cwd=repo, text=True).strip()
    baseline = subprocess.check_output(["git", "show", f"{baseline_commit}:src/treg/infra/money_admission.py"],
                                       cwd=repo, text=True)
    baseline_kv = subprocess.check_output(["git", "show", f"{baseline_commit}:src/treg/infra/kv.py"],
                                          cwd=repo, text=True)
    assert "_local_lock" in baseline and "random.uniform(0.003, 0.010)" in baseline
    assert DELAY in current and GROW in current
    metadata = {"synthetic_only": True, "baseline_ref": args.baseline_ref,
                "baseline_commit": baseline_commit,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "baseline_source_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
                "current_source_sha256": hashlib.sha256(current.encode()).hexdigest(),
                "baseline_kv_source_sha256": hashlib.sha256(baseline_kv.encode()).hexdigest(),
                "current_kv_source_sha256": hashlib.sha256(current_kv.encode()).hexdigest(),
                "python": sys.version, "platform": sys.platform,
                "process_model": "multiprocessing spawn; independent event loops and Redis clients",
                "held_scope": "2 ms, or 2% 200 ms for mixed; no HTTP/PG/accounting",
                "normalization": "CPU and attempts use all completions including drain; throughput uses fixed window",
                "latency_injection": "half before and half after Redis SET/EVAL; within original KV deadlines",
                "execution_order": [],
                "wait_budget_s": 5, "lease_s": 15, "parameters": vars(args) | {"output": str(args.output)}}
    cells = []
    context = mp.get_context("spawn")
    with disposable_redis() as (url, client, info):
        metadata["redis_version"] = info["redis_version"]
        for repeat in range(args.rounds):
            for scenario_index, scenario in enumerate(args.scenarios):
                variants = policy_order(args.variants, repeat, scenario_index, args.balanced_order)
                for position, variant in enumerate(variants):
                    config = {"source": baseline if variant == "local_fixed" else current,
                              "kv_source": baseline_kv if variant == "local_fixed" else current_kv,
                              "variant": variant, "scenario": scenario, "round": repeat,
                              "url": url, "org_base": int(uuid4().hex[:10], 16) * 100,
                              "seconds": args.seconds, "concurrency": args.concurrency,
                              "lease_rtt_ms": args.lease_rtt_ms,
                              "workload_seed": 311 + repeat, "jitter_seed": 7933 + repeat,
                              "execution_index": len(cells), "policy_position": position}
                    result = run_cell(context, config, args.processes, client, args.pressure)
                    cells.append(result)
                    metadata["execution_order"].append({key: config[key] for key in (
                        "execution_index", "round", "scenario", "variant", "policy_position",
                        "workload_seed", "jitter_seed")})
                    print(json.dumps(result["summary"]), flush=True)
                    args.output.write_text(json.dumps({"metadata": metadata, "cells": cells}, indent=2))


if __name__ == "__main__":
    main()
