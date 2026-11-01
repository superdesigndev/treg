"""Observe mode for a shared rate gate on treg's own keys: what it would refuse, against what the
provider answered.

A platform call about to go upstream on a bucket with a declared rate asks the shared window
(`infra/provider_rate.py`) whether it would be admitted, is relayed unchanged either way, and the
verdict is counted against the provider's status. Each process emits one `capacity` line
`rate_observe` per bucket at most once a minute, on that bucket's next call; process exit loses the
counts not yet emitted. `over_2xx` is what enforcing the declared rate would cost: calls the
provider served that the gate would have refused.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from ...config import get_settings
from ...infra import provider_rate
from . import events

FLUSH_S = 60.0


@dataclass(frozen=True)
class Seen:
    provider: str
    bucket: str
    limit: int
    window_s: float
    verdict: Literal["admit", "over", "unknown"]


@dataclass
class _Tally:
    limit: int
    window_s: float
    started: float = field(default_factory=time.monotonic)
    counts: Counter = field(default_factory=Counter)


_tallies: dict[str, _Tally] = {}


def enabled() -> bool:
    return get_settings().provider_rate_mode == "observe"


async def observe(provider: str, bucket: tuple[str, int, float] | None) -> Seen | None:
    if bucket is None or not enabled():
        return None
    key, limit, window_s = bucket
    verdict = await provider_rate.admit(key, limit, window_s)
    return Seen(provider, key, limit, window_s, verdict or "unknown")


def record(seen: Seen | None, status: int | None) -> None:
    """Count the verdict against the provider's status (None: no answer). Never raises."""
    if seen is None:
        return
    try:
        answer = ("none" if status is None else "2xx" if 200 <= status < 300
                  else "429" if status == 429 else "other")
        tally = _tallies.get(seen.bucket)
        if tally is None or (tally.limit, tally.window_s) != (seen.limit, seen.window_s):
            if tally is not None:
                _emit(seen.provider, seen.bucket, tally)
            tally = _tallies[seen.bucket] = _Tally(seen.limit, seen.window_s)
        tally.counts[f"{seen.verdict}_{answer}"] += 1
        if time.monotonic() - tally.started >= FLUSH_S:
            _emit(seen.provider, seen.bucket, tally)
            del _tallies[seen.bucket]
    except Exception:  # noqa: BLE001 - an observation is for the operator, never this call's fate
        pass


def _emit(provider: str, bucket: str, tally: _Tally) -> None:
    events.emit("rate_observe", provider=provider, bucket=bucket, limit=tally.limit,
                window_s=tally.window_s,
                observed_s=round(time.monotonic() - tally.started, 1), **dict(tally.counts))


def reset() -> None:
    _tallies.clear()
