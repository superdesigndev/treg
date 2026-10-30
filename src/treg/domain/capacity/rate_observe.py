"""A shared rate window on treg's own keys: observed on every bucket, enforced on the listed ones.

A platform call about to go upstream on a bucket with a declared rate asks the shared window
(`infra/provider_rate.py`) whether it would be admitted. On a bucket named in
`provider_rate_enforce` a full window refuses the call (`refuses`); everywhere else it is relayed
unchanged. Every verdict is counted against what happened: the provider's status, or `refused`.
Each process emits one `capacity` line `rate_observe` per bucket and rate about once a minute
(`flush_worker`) and once more at shutdown; an abrupt exit loses the counts not yet emitted.
`over_2xx` is what enforcing the declared rate costs: calls the provider served that the window
would refuse. `admit_429` says the declared rate is too high only while `over_*` is near zero:
calls the window would refuse still spend the provider's allowance when they are relayed.
"""
from __future__ import annotations

import asyncio
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
    enforced: bool = False
    retry_after_s: int = 0
    admission: provider_rate.Admission | None = None

    @property
    def refuses(self) -> bool:
        return self.enforced and self.verdict == "over"


@dataclass
class _Tally:
    provider: str
    started: float = field(default_factory=time.monotonic)
    counts: Counter = field(default_factory=Counter)


_tallies: dict[tuple[str, int, float], _Tally] = {}


def enabled() -> bool:
    return get_settings().provider_rate_mode == "observe"


def _enforced(bucket: str) -> bool:
    names = get_settings().provider_rate_enforce
    return bool(names) and bucket.split("#", 1)[0] in {n.strip() for n in names.split(",")}


async def observe(provider: str, bucket: tuple[str, int, float] | None) -> Seen | None:
    if bucket is None or not enabled():
        return None
    key, limit, window_s = bucket
    admission = await provider_rate.admit(key, limit, window_s)
    return Seen(provider, key, limit, window_s, admission.verdict if admission else "unknown",
                _enforced(key), admission.retry_after_s if admission else 0, admission)


def release(seen: Seen | None) -> None:
    """The call was refused for another reason before it was sent: it takes no room and no count."""
    if seen is not None:
        provider_rate.release(seen.admission)


def record(seen: Seen | None, status: int | Literal["refused"] | None) -> None:
    """Count the verdict against what happened (None: the provider gave no answer). Never raises."""
    if seen is None:
        return
    try:
        if status == "refused" or status is None:
            answer = status or "none"
        else:
            answer = "2xx" if 200 <= status < 300 else "429" if status == 429 else "other"
        tally = _tallies.setdefault((seen.bucket, seen.limit, seen.window_s), _Tally(seen.provider))
        tally.counts[f"{seen.verdict}_{answer}"] += 1
    except Exception:  # noqa: BLE001 - an observation is for the operator, never this call's fate
        pass


def flush(*, due_only: bool = False) -> None:
    """Emit each bucket's counts (only those a minute old when `due_only`) and start it afresh."""
    now = time.monotonic()
    for key, tally in list(_tallies.items()):
        if due_only and now - tally.started < FLUSH_S:
            continue
        del _tallies[key]
        if tally.counts:
            bucket, limit, window_s = key
            events.emit("rate_observe", provider=tally.provider, bucket=bucket, limit=limit,
                        window_s=window_s, enforced=_enforced(bucket),
                        observed_s=round(now - tally.started, 1), **dict(tally.counts))


async def flush_worker() -> None:
    while True:
        await asyncio.sleep(FLUSH_S / 4)
        try:
            flush(due_only=True)
        except Exception:  # noqa: BLE001 - a bad pass never kills the loop
            pass


def reset() -> None:
    _tallies.clear()
    provider_rate.reset()
