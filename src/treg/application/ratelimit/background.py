"""The rate limiter's background work, started by every role's lifespan.

- `config_loop`: reads the newest policy row from Postgres every `rate_limit_config_refresh_s`.
- `blocklist_loop`: polls the blocklist version in Postgres every `rate_limit_blocklist_refresh_s`
  (`blocklist_sync`), reporting `blocklist_cache_empty` until a list has loaded, and writes this
  process's heartbeat (versions and modes) to `rl:live:<instance>`, which the admin status reads.
- `lease_loop`: renews the concurrency leases of requests still in flight.
- `maintenance_loop`: flushes surge signals and emits the window's telemetry.

A failed pass never ends a loop. Nothing here runs on the request path. Shutdown cancels a loop
between passes: the pass already in flight finishes and closes its session first (`_finish`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ... import analytics
from ...config import get_settings
from ...infra import kv
from ...infra.db import install_checkout_peaks, pool_snapshot, take_checkout_peaks
from ...infra.money_timing import PROCESS_INSTANCE
from . import blocklist_state, blocklist_sync, config_state, config_sync, inflight, metrics, receipts, signals_flush
from .limiter import limiter

log = logging.getLogger("treg.ratelimit")
_MEMORY_SAMPLE_KEYS = 500


def process_status() -> dict:
    state = config_state.current()
    age = config_state.age_s()
    return {
        "process_instance": PROCESS_INSTANCE, "mode": state.config.mode,
        "blocklist_mode": blocklist_state.current().mode if blocklist_state.current() else None,
        "config_version": state.version,
        "config_source": state.source, "config_fallback": state.fallback,
        "config_degraded": state.degraded, "config_age_s": None if age is None else round(age),
        **blocklist_state.status(now=time.time()),
    }


async def emit() -> tuple[list[dict], dict]:
    rows, gauge = metrics.drain()
    gauge.update({**process_status(), "key_cache_size": len(limiter().cache),
                  **config_state.counters(), **blocklist_state.counters()})
    if kv.configured():
        try:
            gauge.update(await kv.store().rl_memory(_MEMORY_SAMPLE_KEYS))
        except kv.Unavailable:
            gauge["rl_memory_unavailable"] = True
    for row in rows:
        analytics.capture(analytics.SERVER_DISTINCT_ID, "ratelimit_decision",
                          {**row, "process_instance": PROCESS_INSTANCE})
        if row["paid"] and row["result"] in ("reject", "shadow_reject", "log_only"):
            # The line a "paid org was refused" alert matches, independent of the analytics sink.
            log.warning("ratelimit_decision %s", json.dumps({"event": "ratelimit_decision", **row},
                                                            separators=(",", ":")))
    # The per-process version/mode metric: one gauge per process per window.
    analytics.capture(analytics.SERVER_DISTINCT_ID, "ratelimit_gauge", gauge)
    log.info("ratelimit_gauge %s", json.dumps({"event": "ratelimit_gauge", **gauge},
                                              separators=(",", ":"), default=str))
    return rows, gauge


async def _finish(awaitable):
    """Await one pass, and if this task is cancelled, let that pass finish first.

    Cancelling a poll while its session is closing deadlocks the sqlite driver's graceful close,
    and the worker's lifespan then never returns. The pass is a short read; the cancellation is
    taken at the sleep that follows.
    """
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception:  # noqa: BLE001 - the pass already failed; shutdown still leaves
            pass
        raise


async def _forever(interval_s: float, step) -> None:
    while True:
        try:
            await _finish(step())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the limiter keeps its last good state
            log.exception("rate limit background pass failed")
        await asyncio.sleep(interval_s)


async def config_step() -> None:
    await config_sync.poll_once()


async def blocklist_step() -> None:
    await blocklist_sync.poll_once()
    blocklist_sync.report_empty()
    if kv.configured():
        try:
            await kv.store().rl_heartbeat(kv.RL_LIVE_PREFIX + PROCESS_INSTANCE,
                                          json.dumps(process_status(), separators=(",", ":")),
                                          int(get_settings().rate_limit_blocklist_refresh_s * 6))
        except kv.Unavailable:
            pass


async def maintenance_step() -> None:
    await signals_flush.flush()
    await emit()


async def config_loop() -> None:
    await _forever(get_settings().rate_limit_config_refresh_s, config_step)


_EMPTY_RETRY_S = 2.0


async def blocklist_loop() -> None:
    """Every refresh interval, or every couple of seconds while no list has loaded yet."""
    while True:
        try:
            await _finish(blocklist_step())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the limiter keeps its last good list
            log.exception("rate limit blocklist pass failed")
        loaded = blocklist_state.current() is not None
        await asyncio.sleep(get_settings().rate_limit_blocklist_refresh_s if loaded else _EMPTY_RETRY_S)


async def maintenance_loop() -> None:
    s = get_settings()
    await asyncio.sleep(s.surge_flush_s)
    await _forever(s.surge_flush_s, maintenance_step)


class InflightGauge:
    """Emits the window's true peaks every `inflight_gauge_emit_s`: in flight is updated on every
    request (`inflight.enter`), DB checkouts on every pool checkout (`install_checkout_peaks`); both
    reset at each emit. No I/O; a failed pass is swallowed."""

    def __init__(self) -> None:
        self.opened = time.monotonic()
        install_checkout_peaks()

    def emit(self) -> dict:
        snapshot = pool_snapshot()
        peaks = take_checkout_peaks(snapshot)
        row = {
            "event": "inflight_gauge", "pid": os.getpid(), "process_instance": PROCESS_INSTANCE,
            "window_s": round(time.monotonic() - self.opened, 1),
            "in_flight": inflight.current(), "in_flight_peak": inflight.take_peak(),
            "overload_cap_enabled": get_settings().overload_cap_enabled,
            "overload_cap_size": get_settings().overload_cap_size,
            **{f"db_{name}_checked_out": r["checked_out"] for name, r in snapshot.items()},
            **{f"db_{name}_peak": peaks.get(name, 0) for name in snapshot},
            **{f"db_{name}_capacity": r["capacity"] for name, r in snapshot.items()},
        }
        self.opened = time.monotonic()
        log.info("inflight_gauge %s", json.dumps(row, separators=(",", ":")))
        analytics.capture(analytics.SERVER_DISTINCT_ID, "inflight_gauge",
                          {k: v for k, v in row.items() if k != "event"})
        return row


async def inflight_loop() -> None:
    s = get_settings()
    if not s.inflight_gauge_enabled:
        return
    gauge = InflightGauge()
    while True:
        await asyncio.sleep(s.inflight_gauge_emit_s)
        try:
            gauge.emit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - telemetry never stops or fails anything
            log.exception("inflight gauge pass failed")


async def lease_loop() -> None:
    """Renew the leases of requests still in flight, well inside the lease TTL, so a stream that
    outlives the TTL keeps counting. A request that ends releases its lease itself."""
    while True:
        ttl = config_state.current().config.concurrency_ttl_s
        await asyncio.sleep(max(1.0, ttl / 3))
        try:
            await limiter().renew_leases()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a missed renewal only risks one undercount window
            log.exception("rate limit lease renewal failed")


async def run() -> None:
    tasks = [asyncio.create_task(loop())
             for loop in (config_loop, blocklist_loop, maintenance_loop, inflight_loop, lease_loop)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await signals_flush.flush()  # the last partial minute
            await receipts.drain()
        except Exception:  # noqa: BLE001
            pass


def reset_for_tests() -> None:
    signals_flush.reset_for_tests()
    blocklist_sync.reset_for_tests()
