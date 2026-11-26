"""The in-process overflow route view: the enabled `OverflowRoute` rows, reloaded on a 60 s TTL by
an explicit `await load()` (before the resolution session opens), read sync and I/O-free by the
child cycle. Same invalidation story as the capacity view (time-based; every replica sees a sync
within one TTL). Read-only: the worker's `overflow sync` is the only writer."""

from __future__ import annotations

import time

from sqlalchemy import select

from ...infra.db import session_maker
from ...models import OverflowRoute
from ..catalog import store as catalog_store
from .routes import route_for

TTL_S = 60.0


class RouteView:
    def __init__(self, ttl_s: float = TTL_S) -> None:
        self._ttl = ttl_s
        self._loaded_at = float("-inf")
        self._routes: list[OverflowRoute] = []

    async def load(self, *, force: bool = False) -> list[OverflowRoute]:
        if not force and time.monotonic() - self._loaded_at < self._ttl:
            return self._routes
        async with session_maker() as db:
            rows = (await db.execute(select(OverflowRoute).where(OverflowRoute.enabled == True))).scalars().all()  # noqa: E712
            db.expunge_all()
        self._routes, self._loaded_at = list(rows), time.monotonic()
        return self._routes

    def for_endpoint(self, endpoint_id: str) -> list[OverflowRoute]:
        """Enabled routes, Orthogonal first. Sync. None for an async job, even before the next sync
        disables its row: the task would live on the aggregator's account (`eligible`)."""
        if (catalog_store.load().by_id.get(endpoint_id) or {}).get("async"):
            return []
        return route_for(self._routes, endpoint_id)

    def invalidate(self) -> None:
        self._loaded_at = float("-inf")


view = RouteView()
