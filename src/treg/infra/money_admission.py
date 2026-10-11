"""Optional money admission outside database sessions; PostgreSQL remains the authority.

A lease reduces contention, never replaces a money lock or proves a transaction has stopped.
Timeouts and store failures fall back to the original database path. Lease loss never cancels
an in-flight transaction. Callers put the entire session (including rollback/close) inside admit,
and do not call admit again inside that scope.

`reserve` and `release` exist only for a session that will touch an org balance row. An empty or
invalid org id on those operations refuses before the body runs, so the caller cannot open that
session. Wait and key-value failures still enter the body; they are not an HTTP 429.
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from ..config import get_settings
from . import kv
from .money_timing import PROCESS_INSTANCE

_OPERATIONS = frozenset({"close", "deferred", "async", "hub", "reserve", "release"})
# Sessions that update an org balance row. A missing org must not fall through into checkout.
_BALANCE_OPS = frozenset({"reserve", "release"})
_MODES = frozenset({"disabled", "redis", "fallback"})
_REASONS = frozenset({"wait_timeout", "kv_unavailable", "kv_not_configured",
                      "lease_lost", "missing_identity", "gate_error"})
_BOUNDS_MS = (10, 50, 100, 250, 1000, 5000)
_local_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_local_loop = None
_clock = time.monotonic
_metrics_lock = threading.Lock()
_opened = _clock()


@dataclass
class _Window:
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    wait_total_ms: float = 0.0
    wait_max_ms: float = 0.0
    execution_total_ms: float = 0.0
    execution_max_ms: float = 0.0
    total_total_ms: float = 0.0
    total_max_ms: float = 0.0
    waiting_peak: int = 0
    active_peak: int = 0
    lease_lost: int = 0
    kv_errors: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(_BOUNDS_MS) + 1))
    fallbacks: dict[str, int] = field(default_factory=dict)


_windows: dict[tuple[str, str], _Window] = {}
_current: dict[tuple[str, str], dict[str, int]] = {}


class _Observation:
    def __init__(self, operation: str):
        self.operation = operation if operation in _OPERATIONS else None
        self.mode = "disabled"
        self.state: str | None = None
        self.started = 0.0
        self.entered: float | None = None
        self.reason: str | None = None
        self.lease_lost = 0
        self.kv_errors = 0
        try:
            self.started = _clock()
        except Exception:  # noqa: BLE001
            pass

    def transition(self, mode: str, state: str | None) -> None:
        try:
            if self.operation is None or mode not in _MODES:
                return
            with _metrics_lock:
                if self.state:
                    previous = _current[(self.operation, self.mode)]
                    previous[self.state] = max(0, previous[self.state] - 1)
                self.mode, self.state = mode, state
                if state:
                    key = (self.operation, mode)
                    counts = _current.setdefault(key, {"waiting": 0, "active": 0})
                    counts[state] += 1
                    window = _windows.setdefault(key, _Window())
                    setattr(window, state + "_peak", max(
                        getattr(window, state + "_peak"), counts[state]))
        except Exception:  # noqa: BLE001 - diagnostics cannot change admission or money
            pass

    def enter(self, mode: str) -> None:
        try:
            self.entered = _clock()
        except Exception:  # noqa: BLE001
            pass
        self.transition(mode, "active")

    def finish(self, exc: BaseException | None, execution_end: float) -> None:
        self.transition(self.mode, None)
        try:
            if self.operation is None:
                return
            now = _clock()
            wait_ms = max(0.0, (self.entered if self.entered is not None else now) - self.started) * 1000
            execution_ms = (max(0.0, execution_end - self.entered) * 1000
                            if self.entered is not None else 0.0)
            total_ms = max(0.0, now - self.started) * 1000
            with _metrics_lock:
                row = _windows.setdefault((self.operation, self.mode), _Window())
                row.completed += 1
                row.failed += exc is not None
                row.cancelled += isinstance(exc, asyncio.CancelledError)
                for name, value in (("wait", wait_ms), ("execution", execution_ms),
                                    ("total", total_ms)):
                    setattr(row, name + "_total_ms", getattr(row, name + "_total_ms") + value)
                    setattr(row, name + "_max_ms", max(getattr(row, name + "_max_ms"), value))
                row.buckets[next((i for i, bound in enumerate(_BOUNDS_MS)
                                  if wait_ms <= bound), len(_BOUNDS_MS))] += 1
                row.lease_lost += self.lease_lost
                row.kv_errors += self.kv_errors
                if self.reason in _REASONS:
                    row.fallbacks[self.reason] = row.fallbacks.get(self.reason, 0) + 1
        except Exception:  # noqa: BLE001
            pass


def _local_lock(org_id: int) -> asyncio.Lock:
    global _local_locks, _local_loop
    loop = asyncio.get_running_loop()
    if _local_loop is not loop:
        _local_locks = weakref.WeakValueDictionary()
        _local_loop = loop
    lock = _local_locks.get(org_id)
    if lock is None:
        lock = asyncio.Lock()
        _local_locks[org_id] = lock
    return lock


class _Gate:
    def __init__(self, observation: _Observation, lease_s: float):
        self.observation = observation
        self.lease_s = lease_s
        self.ttl_ms = max(1, int(lease_s * 1000))
        self.local: list[asyncio.Lock] = []
        self.leases: list[tuple[str, str]] = []
        # Record before SET: a cancelled/timed-out response may still have acquired the lease.
        self.attempted: list[tuple[str, str]] = []
        self.store: kv.Store | None = None
        self.renewer: asyncio.Task | None = None
        self.lost = False

    async def acquire(self, ids: list[int], wait_s: float) -> str | None:
        async with asyncio.timeout(wait_s):
            for org_id in ids:
                lock = _local_lock(org_id)
                await lock.acquire()
                self.local.append(lock)
            if not kv.configured():
                return "kv_not_configured"
            self.store = kv.store()
            for org_id in ids:
                key, token = f"money-admission:org:{org_id}", uuid.uuid4().hex
                self.attempted.append((key, token))
                while True:
                    result = await self.store.acquire_lease(key, token, self.ttl_ms)
                    if result == "acquired":
                        self.leases.append((key, token))
                        if self.renewer is None:
                            self.renewer = asyncio.create_task(self._renew())
                        break
                    if result != "busy":
                        self.observation.kv_errors += 1
                        return "kv_unavailable"
                    if self.lost:
                        return "lease_lost"
                    # Only the local head waiter polls Redis for an org. Keep handoff delay
                    # short relative to a money transaction without a request-wide poll storm.
                    await asyncio.sleep(random.uniform(0.003, 0.010))
            return "lease_lost" if self.lost else None

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.01, self.lease_s / 3))
                for key, token in tuple(self.leases):
                    result = await self.store.renew_lease(key, token, self.ttl_ms)
                    if result != "renewed":
                        if result == "unavailable":
                            self.observation.kv_errors += 1
                        self.lost = True
                        self.observation.lease_lost = 1
                        return  # Do not cancel the transaction or reacquire a lost lease.
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - lost isolation, never a reason to interrupt accounting
            self.observation.kv_errors += 1
            self.observation.lease_lost = 1
            self.lost = True

    async def release(self) -> None:
        if self.renewer is not None:
            self.renewer.cancel()
            await asyncio.gather(self.renewer, return_exceptions=True)
            self.renewer = None
        try:
            if self.store is not None:
                for key, token in reversed(self.attempted):
                    try:
                        result = await self.store.release_lease(key, token)
                        if result == "unavailable":
                            self.observation.kv_errors += 1
                        elif result == "lost" and (key, token) in self.leases:
                            # A paused event loop can outlive the TTL before renewal runs.
                            self.observation.lease_lost = 1
                    except Exception:  # noqa: BLE001 - TTL remains the last cleanup bound
                        self.observation.kv_errors += 1
        finally:
            self.attempted.clear()
            self.leases.clear()
            for lock in reversed(self.local):
                lock.release()
            self.local.clear()


class AdmissionRefused(Exception):
    """Reserve or release refused before checkout: no positive org id.

    Not an HTTP status. A wait timeout or an unavailable key-value store still runs the
    original database path and must not be turned into a 429 here.
    """


def _invalid_org(org_id: object) -> bool:
    return not isinstance(org_id, int) or isinstance(org_id, bool) or org_id <= 0


def require_balance_orgs(org_ids: Iterable[int | None]) -> list[int]:
    """Orgs whose balance row the next session will touch, or refuse with no session.

    Empty, missing, boolean, and non-positive ids are the same failure: the caller must
    not open a balance session under an empty admit.
    """
    raw = list(org_ids)
    ids = [org_id for org_id in raw if isinstance(org_id, int) and not isinstance(org_id, bool) and org_id > 0]
    if not raw or len(ids) != len(raw):
        raise AdmissionRefused("missing_identity")
    return sorted(set(ids))


async def _release_safely(gate: _Gate) -> None:
    """Join bounded cleanup even under repeated cancellation, then propagate cancellation."""
    task = asyncio.create_task(gate.release())
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    task.result()
    if cancelled is not None:
        raise cancelled


@asynccontextmanager
async def admit(org_ids: Iterable[int | None], *, operation: str) -> AsyncIterator[None]:
    raw = list(org_ids)
    if operation in _BALANCE_OPS and (not raw or any(_invalid_org(org_id) for org_id in raw)):
        # Do not yield. `async with admit(), session()` must not open the session.
        raise AdmissionRefused("missing_identity")
    if not raw:
        yield  # No payer to queue (a close/deferred/async/hub path that will not touch a balance row).
        return
    observation = _Observation(operation)
    gate: _Gate | None = None
    failure: BaseException | None = None
    execution_end = 0.0
    try:
        settings = get_settings()
        mode = "disabled"
        if any(not isinstance(org_id, int) or isinstance(org_id, bool) or org_id <= 0
               for org_id in raw):
            mode, observation.reason = "fallback", "missing_identity"
        elif settings.money_admission_enabled:
            allowed = set(settings.money_admission_org_ids)
            ids = sorted({org_id for org_id in raw if not allowed or org_id in allowed})
            if ids:
                observation.transition("redis", "waiting")
                gate = _Gate(observation, settings.money_admission_lease_s)
                try:
                    reason = await gate.acquire(ids, settings.money_admission_wait_s)
                except TimeoutError:
                    reason = "wait_timeout"
                except Exception:  # noqa: BLE001 - the gate cannot turn a charge into a free call
                    observation.kv_errors += 1
                    reason = "gate_error"
                if reason:
                    observation.reason = reason
                    mode = "fallback"
                    await _release_safely(gate)
                    gate = None
                else:
                    mode = "redis"
        observation.enter(mode)
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            execution_end = _clock()
        except Exception:  # noqa: BLE001
            pass
        try:
            if gate is not None:
                await _release_safely(gate)
        except asyncio.CancelledError as exc:
            if failure is None:
                failure = exc
                raise
        finally:
            observation.finish(failure, execution_end)


def snapshot() -> list[dict]:
    """Drain bounded completion windows, retaining current waiters and active session scopes."""
    global _opened, _windows
    now = _clock()
    with _metrics_lock:
        elapsed = max(0.0, now - _opened)
        windows, _windows = _windows, {}
        current = {key: dict(counts) for key, counts in _current.items()}
        for key, counts in current.items():
            if counts["waiting"] or counts["active"]:
                _windows[key] = _Window(waiting_peak=counts["waiting"], active_peak=counts["active"])
        _opened = now
    rows = []
    for (operation, mode), window in windows.items():
        props = {
            "operation": operation, "mode": mode, "process_instance": PROCESS_INSTANCE,
            "window_s": round(elapsed, 3),
            **{name: round(getattr(window, name), 3) for name in (
                "completed", "failed", "cancelled", "wait_total_ms", "wait_max_ms",
                "execution_total_ms", "execution_max_ms", "total_total_ms", "total_max_ms",
                "waiting_peak", "active_peak", "lease_lost", "kv_errors")},
            **current.get((operation, mode), {"waiting": 0, "active": 0}),
        }
        for i, bound in enumerate(_BOUNDS_MS):
            props[f"wait_bucket_le_{bound}_ms"] = window.buckets[i]
        props["wait_bucket_gt_5000_ms"] = window.buckets[-1]
        for reason in sorted(_REASONS):
            props[f"fallback_{reason}"] = window.fallbacks.get(reason, 0)
        rows.append(props)
    return rows
