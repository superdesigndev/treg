"""Per-provider token bucket for treg's OWN platform keys — burst smoothing (plan §4.4).

Burst-429s on tier 4 (leadsforge 27% of calls, crustdata 34%, leadmagic bursting to 26 req/s
against a 300/min limit) are self-inflicted concurrency, not capacity: many callers sharing one
key. A call that would exceed the provider's published rate waits — briefly, bounded — instead of
being relayed into a 429 the caller cannot fix. The hold is already placed, so the wait costs the
org latency, never money; beyond `max_wait_ms` the call proceeds as today (no refusal here, ever).

In-process and DB-free on purpose: this runs between "DB phase ended" and the relay, where the
call must hold no connection. A second replica doubles the effective rate; the `rate_pressure`
alert (step C) is the signal to tighten, not a shared counter on the request path.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

DEFAULT_MAX_WAIT_MS = 2_000


@dataclass
class _Bucket:
    limit: int
    window_s: float
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)

    # Capacity ONE: a spacer, not a burst allowance. Providers count sliding windows, and a burst
    # of `limit` calls at t=0 — legal for a classic bucket — is exactly what they 429. One call per
    # `window_s / limit` is the shape every counter accepts; the cost is a wait of at most one
    # interval per queued call (0.5 s at 120/min, 33 ms at 30/s), bounded by `max_wait_ms`.
    CAPACITY = 1.0

    def __post_init__(self) -> None:
        self.tokens = self.CAPACITY

    @property
    def rate(self) -> float:  # tokens per second
        return self.limit / self.window_s

    def _refill(self, now: float) -> None:
        self.tokens = min(self.CAPACITY, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def take(self, now: float) -> float:
        """Take one token; returns the seconds to wait before it is honoured (0 = immediately)."""
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        deficit = 1.0 - self.tokens
        self.tokens -= 1.0  # go negative: the queue is the wait
        return deficit / self.rate


class Limiter:
    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def bucket(self, provider: str, limit: int, window_s: float) -> _Bucket:
        b = self._buckets.get(provider)
        if b is None or b.limit != limit or b.window_s != window_s:
            b = self._buckets[provider] = _Bucket(limit, window_s)
        return b

    async def acquire(self, provider: str, limit: int, window_s: float, *,
                      max_wait_ms: int = DEFAULT_MAX_WAIT_MS) -> int:
        """Wait for a token, at most `max_wait_ms`; returns the milliseconds actually waited. A wait
        longer than the cap is NOT taken (the token is returned) — the call proceeds and the provider
        answers as it would have; `rate_pressure` is the alert for that."""
        if limit <= 0 or window_s <= 0:
            return 0
        b = self.bucket(provider, limit, window_s)
        wait_s = b.take(time.monotonic())
        if wait_s <= 0:
            return 0
        if wait_s * 1000 > max_wait_ms:
            b.tokens += 1.0  # give the token back; we are not going to wait for it
            return 0
        await asyncio.sleep(wait_s)
        return int(wait_s * 1000)

    def reset(self) -> None:
        self._buckets.clear()


limiter = Limiter()
"""The process-wide instance (one bucket per provider)."""
