"""The in-process capacity view: what the dataplane reads instead of the capacity tables.

Two ratestore namespaces, one query, cached for `TTL_S`: the sweep's published state
(`capacity:state:*`) and the call path's locks (`capacity:lock:*`). A provider is exhausted when
either says so. Nothing here writes; a writer calls `invalidate()` so its own process sees the
change before the TTL.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from ...infra.db import session_maker
from ...models import Ephemeral
from ...timeutil import utcnow_naive
from .marks import LOCK_NS, Lock, lock_key
from .policy import _CONCURRENCY, _ENDPOINT_RATE_LIMITS, _RATE_LIMITS, LatestState
from .sweep import STATE_NS, state_key

TTL_S = 60.0


class LatestStateView:
    def __init__(self, ttl_s: float = TTL_S) -> None:
        self._ttl = ttl_s
        self._loaded_at = -1.0
        self._states: dict[str, LatestState] = {}
        self._locks: dict[str, Lock] = {}

    async def load(self, *, force: bool = False) -> dict[str, LatestState]:
        if not force and time.monotonic() - self._loaded_at < self._ttl:
            return self._states
        async with session_maker() as db:
            rows = (await db.execute(
                select(Ephemeral).where(Ephemeral.ns.in_((STATE_NS, LOCK_NS)),
                                        Ephemeral.expires_at >= utcnow_naive()))).scalars().all()
        self._states = {r.k: LatestState.from_json(r.v) for r in rows if r.ns == STATE_NS}
        self._locks = {r.k: Lock.from_json(r.v) for r in rows if r.ns == LOCK_NS}
        self._loaded_at = time.monotonic()
        return self._states

    def get(self, provider: str) -> LatestState | None:
        """The sweep's state; call `load()` first. Sync and I/O-free on purpose (resolve.py rule)."""
        return self._states.get(state_key(provider))

    def locks(self, provider: str, endpoint_id: str | None = None,
              slot: str | None = None, *, metered: bool = True) -> list[Lock]:
        """The call path's rows for this call, pending or active: the endpoint's and the provider's.
        A further treg-owned account (`slot`) has its own rows (`marks.lock_key`). A free route
        (`metered=False`, `marks.spends_balance`) spends no balance: only its endpoint's own row
        (a quota) speaks for it, never the provider's balance row."""
        keys = [lock_key(k, None, "", slot) for k in (endpoint_id, provider if metered else None) if k]
        return [lock for k in keys if (lock := self._locks.get(k)) is not None]

    def active_lock(self, provider: str, endpoint_id: str | None = None,
                    slot: str | None = None, *, metered: bool = True) -> Lock | None:
        return next((lock for lock in self.locks(provider, endpoint_id, slot, metered=metered)
                     if lock.is_active()), None)

    def is_exhausted(self, provider: str, endpoint_id: str | None = None,
                     slot: str | None = None, *, metered: bool = True) -> bool:
        return self.exhausted_until(provider, endpoint_id, slot, metered=metered) is not None

    def exhausted_until(self, provider: str, endpoint_id: str | None = None, slot: str | None = None,
                        *, metered: bool = True):
        """When this call serves again per whichever source refuses it now, else None. The sweep
        reads only the provider's own account, so it never speaks for a further one, and only its
        balance, so it never speaks for a free route."""
        lock = self.active_lock(provider, endpoint_id, slot, metered=metered)
        if lock is not None:
            return lock.until
        state = self._states.get(state_key(provider)) if slot is None and metered else None
        return state.exhausted_until if state and state.is_exhausted() else None

    def rate_bucket(self, provider: str, endpoint_id: str | None,
                    slot: str | None = None) -> tuple[str, int, float] | None:
        """(bucket key, limit, window_s) smoothing this platform call, or None when unknown. An
        endpoint with its own allowance (`_ENDPOINT_RATE_LIMITS`) is its own bucket; every other
        endpoint shares the provider's. Each further treg-owned account (`slot`) has its own
        allowance, so its own bucket. Sync, I/O-free."""
        pair = _pair(_ENDPOINT_RATE_LIMITS.get(endpoint_id or ""))
        key = endpoint_id
        if not pair:
            pair, key = self.rate_limit(provider), provider
        if not pair:
            return None
        return (f"{key}#{slot}" if slot else key, *pair)

    def rate_limit(self, provider: str) -> tuple[int, float] | None:
        """(limit, window_s) for the provider's platform key, or None when unknown. Published by the
        sweep from the policy; the verified defaults apply before a sweep has run. Sync, I/O-free."""
        state = self._states.get(state_key(provider))
        return _pair((state.rate_limit if state and state.rate_limit else None)
                     or _RATE_LIMITS.get(provider))

    def concurrency(self, provider: str) -> int:
        """Calls in progress the provider allows on its platform key, or 0 when unknown (no gate)."""
        return _CONCURRENCY.get(provider, 0)

    def invalidate(self) -> None:
        self._loaded_at = -1.0


def _pair(rl: dict | None) -> tuple[int, float] | None:
    """A rate-limit row as (limit, window_s), or None when it does not state both."""
    if not rl or not rl.get("limit") or not rl.get("window_s"):
        return None
    return int(rl["limit"]), float(rl["window_s"])


view = LatestStateView()
"""The process-wide instance."""
