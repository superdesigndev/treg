"""A routed child's health right now, from a short window of its recent answers. Pure.

The long-window endpoint stats (`catalog.stats`) order the plan; they cannot see an outage that
started minutes ago. This reads a few minutes of the child's own answers in routed calls and says
whether to ask it at all (`failing`), ask it after the others (`slow`), or leave it be (`ok`).
"""

from __future__ import annotations

from dataclasses import dataclass

OK, SLOW, FAILING = "ok", "slow", "failing"
# Statuses that say the provider cannot answer right now, whatever the question. 429 and 402 are
# the capacity system's (rate windows, exhausted accounts), never health.
_DOWN_STATUSES = frozenset({405, 408, 410})


@dataclass(frozen=True)
class Tally:
    """Answers in the window: `calls` that reached the provider, `failed` (5xx, timeout, no
    connection), `slow` (answered, but past half the child's time bound)."""

    calls: int = 0
    failed: int = 0
    slow: int = 0

    def __add__(self, other: "Tally") -> "Tally":
        return Tally(self.calls + other.calls, self.failed + other.failed, self.slow + other.slow)


@dataclass(frozen=True)
class Thresholds:
    min_calls: int
    fail_pct: int
    slow_pct: int


def verdict(tally: Tally, t: Thresholds) -> str:
    """`failing` once at least `min_calls` answers are in and `fail_pct` of them failed; `slow`
    once `slow_pct` of them were slow; else `ok`. Too few answers is `ok`: one bad call is noise."""
    if tally.calls < max(1, t.min_calls):
        return OK
    if 100 * tally.failed >= t.fail_pct * tally.calls:
        return FAILING
    if 100 * tally.slow >= t.slow_pct * tally.calls:
        return SLOW
    return OK


def failed_status(status: int | None) -> bool:
    """A provider status that counts against its health: any 5xx, or one of `_DOWN_STATUSES`."""
    return status is not None and (status >= 500 or status in _DOWN_STATUSES)


def child_timeout_s(floor_s: float, cap_s: float, p95_ms: int | None, multiple: float = 2.0) -> float | None:
    """How long one routed child may take: `floor_s`, or `multiple` × its measured p95 when that
    is longer (a slow-but-working scraper keeps its time), never past `cap_s` (the relay's own
    timeout). None when `floor_s` is 0: the bound is off."""
    if floor_s <= 0:
        return None
    measured = (p95_ms or 0) * multiple / 1000.0
    return min(float(cap_s), max(float(floor_s), measured))


def bucket_ids(now_s: float, window_s: int, buckets: int) -> list[int]:
    """The time buckets a window reads, oldest first; the last one is still filling."""
    width = max(1, window_s // max(1, buckets))
    current = int(now_s // width)
    return list(range(current - buckets + 1, current + 1))


def bucket_width(window_s: int, buckets: int) -> int:
    return max(1, window_s // max(1, buckets))
