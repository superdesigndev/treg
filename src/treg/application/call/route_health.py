"""Routed children's health right now, shared across workers through the key-value store.

Every child of a routed call that reaches its provider is counted in a short time bucket
(`kv.note_route_health`): answered, failed (5xx, timeout, no connection) or slow. `build_plan`
reads the window (`domain.catalog.routing.health.verdict`) and skips a treg-key child that is
failing, asking one call per probe interval through anyway, or asks a slow one after the others.
Nothing here is durable: buckets expire with the window, so a child recovers on its own.

Reads go through a short per-process cache, so a routed call adds no store round trip on most
calls; this process's own counts land in the cache at once. A store that cannot answer reads as
no evidence, and every child is `ok`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ...config import get_settings
from ...domain.catalog.routing import health as rules
from ...infra import kv

log = logging.getLogger("treg.route")

BUCKETS = 4
CACHE_TTL_S = 2.0
_cache: dict[str, tuple[float, rules.Tally]] = {}
_writes: set[asyncio.Task] = set()


def mode() -> str:
    return get_settings().route_health


def thresholds() -> rules.Thresholds:
    s = get_settings()
    return rules.Thresholds(s.route_health_min_calls, s.route_health_fail_pct, s.route_health_slow_pct)


def _window() -> tuple[int, int]:
    s = get_settings()
    return s.route_health_window_s, rules.bucket_width(s.route_health_window_s, BUCKETS)


async def read(endpoint_ids: list[str]) -> dict[str, rules.Tally]:
    """The window's tally per endpoint. Never raises; unknown reads as an empty tally."""
    if mode() == "off" or not endpoint_ids:
        return {}
    now = time.monotonic()
    stale = [e for e in endpoint_ids if now - _cache.get(e, (-CACHE_TTL_S - 1, None))[0] > CACHE_TTL_S]
    if stale:
        window_s, _ = _window()
        try:
            got = await kv.store().read_route_health(stale, rules.bucket_ids(time.time(), window_s, BUCKETS))
        except Exception:
            log.warning("route health unavailable", exc_info=True)
            got = None
        for endpoint_id in stale:
            counts = (got or {}).get(endpoint_id)
            if counts is not None:
                _cache[endpoint_id] = (now, rules.Tally(*counts))
            elif got is None:
                _cache.pop(endpoint_id, None)
    return {e: _cache[e][1] for e in endpoint_ids if e in _cache}


def note(endpoint_id: str, *, failed: bool, slow: bool) -> None:
    """Count one answer: in this process's cache now, in the shared store in the background."""
    if mode() == "off":
        return
    at, tally = _cache.get(endpoint_id, (time.monotonic(), rules.Tally()))
    _cache[endpoint_id] = (at, tally + rules.Tally(1, int(failed), int(slow)))
    window_s, width = _window()
    bucket = int(time.time() // width)
    task = asyncio.get_running_loop().create_task(kv.store().note_route_health(
        endpoint_id, bucket, failed=failed, slow=slow, ttl_s=window_s + width))
    _writes.add(task)
    task.add_done_callback(_writes.discard)


async def probe(endpoint_id: str) -> bool:
    """Whether this call may ask a failing child anyway: one call per probe interval, fleet-wide
    where the store is shared. A store that cannot answer grants no probe."""
    try:
        return await kv.store().take(f"treg:route-health-probe:v1:{endpoint_id}", 1,
                                     get_settings().route_health_probe_s)
    except Exception:  # noqa: BLE001
        return False


def reset() -> None:
    """Forget the cache (tests, and a process that changes its settings)."""
    _cache.clear()
