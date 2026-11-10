"""Bounded, in-memory timings for the ordinary call's money session scopes.

No SQL, I/O, awaits or transaction ownership: failures here must never change money behavior.
The scope includes connection acquisition and session cleanup, not just time holding row locks.
Only the web call reserve/close/deferred paths opt in; this is not a count of all money writers.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field


PROCESS_INSTANCE = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
_OPERATIONS = frozenset({"reserve", "close", "deferred"})
_PHASES = frozenset({"preflight", "ledger", "commit", "post_commit"})
_BOUNDS_MS = (10, 50, 100, 250, 1000, 5000)
SLOW_MS = 1000
_lock = threading.Lock()
_clock = time.monotonic
_opened = _clock()
_inflight: dict[str, int] = {}


@dataclass
class _Window:
    count: int = 0
    failed: int = 0
    cancelled: int = 0
    deadlocks: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    batch_items: int = 0
    peak: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(_BOUNDS_MS) + 1))
    phases: dict[str, tuple[float, float]] = field(default_factory=dict)
    slowest: dict | None = None


_windows: dict[str, _Window] = {}


class _Phase:
    def __init__(self, owner: MoneyTiming, name: str):
        self.owner = owner
        self.name = name
        self.started = 0.0

    def __enter__(self):
        try:
            self.started = _clock()
        except Exception:  # noqa: BLE001
            pass
        return self

    def __exit__(self, *_):
        try:
            if self.owner.active and self.name in _PHASES:
                ms = (_clock() - self.started) * 1000
                self.owner.phases[self.name] = self.owner.phases.get(self.name, 0.0) + ms
        except Exception:  # noqa: BLE001 - telemetry must not replace the operation's outcome
            pass
        return False


class MoneyTiming:
    def __init__(self, operation: str, *, org_id: int | None = None,
                 call_id: str | None = None, batch_size: int = 1):
        self.operation = operation
        self.org_id = org_id if isinstance(org_id, int) else None
        # Only opaque identifiers go to the sampled log, never arbitrary request content.
        self.call_id = (call_id if isinstance(call_id, str)
                        and re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", call_id) else None)
        self.batch_size = batch_size if isinstance(batch_size, int) and batch_size >= 0 else 0
        self.phases: dict[str, float] = {}
        self.started = 0.0
        self.active = False

    def __enter__(self):
        try:
            self.started = _clock()
            if self.operation in _OPERATIONS:
                with _lock:
                    n = _inflight.get(self.operation, 0) + 1
                    _inflight[self.operation] = n
                    window = _windows.setdefault(self.operation, _Window())
                    window.peak = max(window.peak, n)
                self.active = True
        except Exception:  # noqa: BLE001
            pass
        return self

    def phase(self, name: str) -> _Phase:
        return _Phase(self, name)

    def __exit__(self, exc_type, exc, _tb):
        try:
            if self.active:
                ms = max(0.0, (_clock() - self.started) * 1000)
                with _lock:
                    _inflight[self.operation] = max(0, _inflight.get(self.operation, 1) - 1)
                    window = _windows.setdefault(self.operation, _Window())
                    window.count += 1
                    window.failed += exc_type is not None
                    window.cancelled += isinstance(exc, asyncio.CancelledError)
                    window.deadlocks += getattr(getattr(exc, "orig", None), "sqlstate", None) == "40P01"
                    window.total_ms += ms
                    window.max_ms = max(window.max_ms, ms)
                    window.batch_items += self.batch_size
                    bucket = next((i for i, bound in enumerate(_BOUNDS_MS) if ms <= bound),
                                  len(_BOUNDS_MS))
                    window.buckets[bucket] += 1
                    for phase, elapsed in self.phases.items():
                        total, maximum = window.phases.get(phase, (0.0, 0.0))
                        window.phases[phase] = (total + elapsed, max(maximum, elapsed))
                    if ms >= SLOW_MS and (window.slowest is None or ms > window.slowest["duration_ms"]):
                        window.slowest = {
                            "duration_ms": round(ms, 3), "org_id": self.org_id,
                            "call_id": self.call_id, "batch_size": self.batch_size,
                            "outcome": "failed" if exc_type is not None else "completed",
                            "phases_ms": {name: round(value, 3) for name, value in self.phases.items()},
                        }
        except Exception:  # noqa: BLE001
            pass
        return False


def observe_money(operation: str, *, org_id: int | None = None,
                  call_id: str | None = None, batch_size: int = 1) -> MoneyTiming:
    return MoneyTiming(operation, org_id=org_id, call_id=call_id, batch_size=batch_size)


def snapshot() -> list[tuple[dict, dict | None]]:
    """Drain completed-operation aggregates; keep active scopes across window boundaries.

    Histogram buckets are disjoint; the last bucket is greater than 5000 ms. Timings belong to
    the completion window, even if an operation started earlier. In-flight counts are session
    scopes, not checked-out database connections. The caller emits at most once per minute.
    """
    global _opened, _windows
    now = _clock()
    with _lock:
        window_s = max(0.0, now - _opened)
        windows, _windows = _windows, {
            operation: _Window(peak=count) for operation, count in _inflight.items() if count
        }
        inflight = dict(_inflight)
        _opened = now
    rows = []
    for operation, window in windows.items():
        props = {
            "operation": operation, "process_instance": PROCESS_INSTANCE,
            "window_s": round(window_s, 3), "completed": window.count,
            "failed": window.failed, "cancelled": window.cancelled, "deadlocks": window.deadlocks,
            "duration_total_ms": round(window.total_ms, 3), "duration_max_ms": round(window.max_ms, 3),
            "batch_items": window.batch_items, "inflight": inflight.get(operation, 0),
            "inflight_peak": window.peak,
        }
        for i, bound in enumerate(_BOUNDS_MS):
            props[f"duration_bucket_le_{bound}_ms"] = window.buckets[i]
        props["duration_bucket_gt_5000_ms"] = window.buckets[-1]
        for name, (total, maximum) in window.phases.items():
            props[f"{name}_total_ms"] = round(total, 3)
            props[f"{name}_max_ms"] = round(maximum, 3)
        rows.append((props, window.slowest))
    return rows
