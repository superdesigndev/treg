"""Process cache and Postgres adapter for Catalog endpoint observations.

Catalog reliability is an optional, lossy read model. A cold cache must never make an open Catalog
request wait for Postgres, while a search burst must never turn one slow aggregate into one checked
out connection per request. This adapter therefore serves fresh or stale endpoint entries
immediately and owns one process-level refresh task for every concurrent caller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..domain.catalog import stats

FRESH_TTL_S = 5 * 60
STALE_TTL_S = 30 * 60
REFRESH_RETRY_S = 5

log = logging.getLogger("treg.catalog")


@dataclass(frozen=True)
class EndpointObservationCacheCounts:
    """Entry-level cache counters plus refresh attempts and failures."""

    fresh: int
    stale: int
    miss: int
    refresh: int
    refresh_failure: int


@dataclass(frozen=True)
class _Entry:
    value: stats.EndpointObservation
    stored_at: float


class PostgresEndpointObservationReader:
    """Authoritative reader whose session exists only for the two aggregate queries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_many(self, endpoint_ids: Collection[str]) -> stats.ObservationSnapshot:
        ids = list(dict.fromkeys(endpoint_ids))
        if not ids:
            return {}
        from ..domain.catalog import store as catalog_store  # the per-success set is a catalog fact, read at query time
        cat = catalog_store.load()
        per_success = {i for i in ids if ((cat.by_id.get(i) or {}).get("cost") or {}).get("type") == "per_success"}
        async with self._session_factory() as db:
            return await stats.observed(db, ids, per_success=per_success)


class CachedEndpointObservationReader:
    """Endpoint-level stale-while-revalidate cache with process-level singleflight.

    Reads never await the source. A fresh entry is at most five minutes old; an entry between five
    and thirty minutes old is returned while refresh runs; a missing or older entry is omitted until
    refresh succeeds. One shared task batches concurrent misses, and endpoint ids already in flight
    are not queued a second time.
    """

    def __init__(
        self,
        source: stats.EndpointObservationReader,
        *,
        clock: Callable[[], float] = time.monotonic,
        fresh_ttl_s: float = FRESH_TTL_S,
        stale_ttl_s: float = STALE_TTL_S,
        retry_s: float = REFRESH_RETRY_S,
    ) -> None:
        if fresh_ttl_s < 0 or stale_ttl_s < fresh_ttl_s:
            raise ValueError("cache TTLs must satisfy 0 <= fresh <= stale")
        self._source = source
        self._clock = clock
        self._fresh_ttl_s = fresh_ttl_s
        self._stale_ttl_s = stale_ttl_s
        self._retry_s = retry_s
        self._entries: dict[str, _Entry] = {}
        self._pending: set[str] = set()
        self._inflight: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._retry_not_before = 0.0
        self._fresh = 0
        self._stale = 0
        self._miss = 0
        self._refresh = 0
        self._refresh_failure = 0

    @property
    def counts(self) -> EndpointObservationCacheCounts:
        return EndpointObservationCacheCounts(
            fresh=self._fresh,
            stale=self._stale,
            miss=self._miss,
            refresh=self._refresh,
            refresh_failure=self._refresh_failure,
        )

    async def get_many(self, endpoint_ids: Collection[str]) -> stats.ObservationSnapshot:
        ids = [endpoint_id for endpoint_id in dict.fromkeys(endpoint_ids) if endpoint_id]
        if not ids:
            return {}

        now = self._clock()
        result: stats.ObservationSnapshot = {}
        refresh_ids: set[str] = set()
        async with self._lock:
            for endpoint_id in ids:
                entry = self._entries.get(endpoint_id)
                age = now - entry.stored_at if entry is not None else None
                if entry is not None and age is not None and age <= self._fresh_ttl_s:
                    self._fresh += 1
                    result[endpoint_id] = entry.value
                elif entry is not None and age is not None and age <= self._stale_ttl_s:
                    self._stale += 1
                    result[endpoint_id] = entry.value
                    refresh_ids.add(endpoint_id)
                else:
                    self._miss += 1
                    refresh_ids.add(endpoint_id)
                    if entry is not None:
                        del self._entries[endpoint_id]

            if not self._closed and refresh_ids:
                self._pending.update(refresh_ids - self._inflight)
                self._start_refresh_locked(now)
        return result

    def _start_refresh_locked(self, now: float) -> None:
        if (self._task is None and self._pending and now >= self._retry_not_before
                and not self._closed):
            self._task = asyncio.create_task(
                self._refresh_loop(), name="catalog-endpoint-observations-refresh"
            )

    async def _refresh_loop(self) -> None:
        try:
            while True:
                async with self._lock:
                    if (self._closed or not self._pending
                            or self._clock() < self._retry_not_before):
                        return
                    endpoint_ids = tuple(sorted(self._pending))
                    self._pending.clear()
                    self._inflight.update(endpoint_ids)
                    self._refresh += 1

                try:
                    refreshed = await self._source.get_many(endpoint_ids)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - optional telemetry always degrades to stale/empty
                    async with self._lock:
                        self._refresh_failure += 1
                        self._retry_not_before = self._clock() + self._retry_s
                    log.warning("endpoint stats refresh unavailable", exc_info=True)
                else:
                    stored_at = self._clock()
                    async with self._lock:
                        for endpoint_id, value in refreshed.items():
                            if endpoint_id in self._inflight:
                                self._entries[endpoint_id] = _Entry(value=value, stored_at=stored_at)
                        self._retry_not_before = 0.0
                finally:
                    async with self._lock:
                        self._inflight.difference_update(endpoint_ids)
        finally:
            async with self._lock:
                if self._task is asyncio.current_task():
                    self._task = None

    async def wait_for_idle(self) -> None:
        """Wait for the current shared refresh task, primarily for orderly tests and shutdown."""
        async with self._lock:
            task = self._task
        if task is not None:
            await asyncio.shield(task)

    async def reset(self) -> None:
        """Clear process state, used when an owning database is reset in tests."""
        await self._cancel_task(close=False)
        async with self._lock:
            self._entries.clear()
            self._pending.clear()
            self._inflight.clear()
            self._retry_not_before = 0.0
            self._fresh = self._stale = self._miss = 0
            self._refresh = self._refresh_failure = 0

    async def aclose(self) -> None:
        """Stop accepting refresh work and cancel the task owned by this app instance."""
        await self._cancel_task(close=True)

    async def _cancel_task(self, *, close: bool) -> None:
        async with self._lock:
            self._closed = close
            self._pending.clear()
            task = self._task
            if task is not None:
                task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
