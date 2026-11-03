"""Reads the verdict-quality file the router uses under `X-Treg-Route-Decisive`.

One small JSON object per capability (`<prefix><capability>/routing.json`) in a private R2 bucket,
written by an operator's scorer. Cached per process; a request never waits longer than
FIRST_READ_WAIT_S for it, and any failure means "no data": the router keeps its price order."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from ..domain.catalog.routing import quality

log = logging.getLogger("treg.route_quality")
FIRST_READ_WAIT_S = 1.0
MAX_BYTES = 1 << 20


class RouteQualityCache:
    def __init__(self, fetch: Callable[[str], Awaitable[bytes | None]], ttl_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch, self._ttl, self._clock = fetch, ttl_s, clock
        self._cache: dict[str, tuple[float, quality.Snapshot | None]] = {}
        self._inflight: dict[str, asyncio.Task] = {}

    async def get(self, capability: str) -> quality.Snapshot | None:
        entry = self._cache.get(capability)
        if entry is not None and self._clock() - entry[0] < self._ttl:
            return entry[1]
        task = self._inflight.get(capability)
        if task is None:
            task = self._inflight[capability] = asyncio.create_task(self._refresh(capability))
        if entry is not None:
            return entry[1]   # stale is better than waiting; the refresh runs behind this call
        try:
            return await asyncio.wait_for(asyncio.shield(task), FIRST_READ_WAIT_S)
        except asyncio.TimeoutError:
            return None

    def warm(self, capabilities) -> None:
        """Start reading each capability's file now, so the first routed call after a deploy is
        ordered by quality rather than falling back to price while the read is still in flight."""
        for capability in capabilities:
            if capability not in self._cache and capability not in self._inflight:
                self._inflight[capability] = asyncio.create_task(self._refresh(capability))

    async def _refresh(self, capability: str) -> quality.Snapshot | None:
        try:
            body = await self._fetch(capability)
            value = quality.parse(json.loads(body)) if body else None
        except Exception as exc:  # noqa: BLE001 - no data is the safe answer; keys stay out of logs
            log.warning("event=route_quality_unavailable capability=%s error=%s", capability, type(exc).__name__)
            value = self._cache.get(capability, (0.0, None))[1]
        self._cache[capability] = (self._clock(), value)
        self._inflight.pop(capability, None)
        return value


def from_settings(settings) -> RouteQualityCache | None:
    """None unless the bucket, its endpoint, keys and prefix are all set."""
    if not (settings.route_quality_endpoint and settings.route_quality_bucket and settings.route_quality_prefix
            and settings.route_quality_access_key_id and settings.route_quality_secret_access_key):
        return None
    from obstore.store import S3Store

    client = S3Store(settings.route_quality_bucket, config={
        "endpoint": settings.route_quality_endpoint, "region": "auto",
        "access_key_id": settings.route_quality_access_key_id,
        "secret_access_key": settings.route_quality_secret_access_key,
    }, client_options={"timeout": "5s", "connect_timeout": "5s"}, retry_config={"max_retries": 0})

    async def fetch(capability: str) -> bytes | None:
        try:
            result = await client.get_async(f"{settings.route_quality_prefix}{capability}/routing.json")
        except FileNotFoundError:
            return None
        if result.meta["size"] > MAX_BYTES:
            raise ValueError("routing quality file too large")
        return bytes(await result.bytes_async())

    return RouteQualityCache(fetch, settings.route_quality_ttl_s)
