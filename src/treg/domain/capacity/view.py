"""The in-process latest-state view: what the dataplane reads instead of the capacity tables.

Loaded from ratestore (`capacity:state:*`) on a short session, cached for `TTL_S`. Invalidation
story (refactor plan §2.2): time-based only — a sweep or a call-path mark lands in ratestore, and
every replica sees it within one TTL. Nothing here writes. Step B ships the loader; the call path
starts consulting it in step D.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from ...infra.db import session_maker
from ...models import Ephemeral
from ...timeutil import utcnow_naive
from .policy import _RATE_LIMITS, LatestState
from .sweep import STATE_NS

TTL_S = 60.0


class LatestStateView:
    def __init__(self, ttl_s: float = TTL_S) -> None:
        self._ttl = ttl_s
        self._loaded_at = -1.0
        self._states: dict[str, LatestState] = {}

    async def load(self, *, force: bool = False) -> dict[str, LatestState]:
        if not force and time.monotonic() - self._loaded_at < self._ttl:
            return self._states
        async with session_maker() as db:
            rows = (await db.execute(
                select(Ephemeral).where(Ephemeral.ns == STATE_NS,
                                        Ephemeral.expires_at >= utcnow_naive()))).scalars().all()
            states = {r.k: LatestState.from_json(r.v) for r in rows}
        self._states, self._loaded_at = states, time.monotonic()
        return states

    def get(self, provider: str) -> LatestState | None:
        """The cached state; call `load()` first. Sync and I/O-free on purpose (resolve.py rule)."""
        return self._states.get(provider)

    def is_exhausted(self, provider: str) -> bool:
        state = self._states.get(provider)
        return bool(state and state.is_exhausted())

    def rate_limit(self, provider: str) -> tuple[int, float] | None:
        """(limit, window_s) for the provider's platform key, or None when unknown. Published by the
        sweep from the policy; the verified defaults apply before a sweep has run. Sync, I/O-free."""
        state = self._states.get(provider)
        rl = (state.rate_limit if state and state.rate_limit else None) or _RATE_LIMITS.get(provider)
        if not rl or not rl.get("limit") or not rl.get("window_s"):
            return None
        return int(rl["limit"]), float(rl["window_s"])

    def invalidate(self) -> None:
        self._loaded_at = -1.0


view = LatestStateView()
"""The process-wide instance."""
