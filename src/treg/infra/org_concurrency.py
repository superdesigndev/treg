"""Optional Redis rate/concurrency admission, independent of money admission/locks.

A lease bounds abandoned occupancy; it cannot fence an already-running provider request.
On renewal loss, keep accepted work and its financial cleanup alive and report the lost cap.
Published configuration supplies the limits; unavailable Redis allows new admissions.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import anyio

from . import kv, org_flow_control as flow

log = logging.getLogger("treg.org_concurrency")
_LEASE_S = 30
_RETRY_AFTER_S = 1
_next_unavailable_log = 0.0
_suppressed_unavailable_logs = 0
_next_rate_log = 0.0
_suppressed_rate_logs = 0


class AdmissionRejected(Exception):
    def __init__(self, *, retry_after_s: int, version: int, rate_limited: bool = False) -> None:
        self.status_code = 429
        self.retry_after_s = retry_after_s
        self.detail = {
            "error": "org_rate_limit" if rate_limited else "org_concurrency_limit",
            "message": ("organization request rate limit reached" if rate_limited else
                        "organization concurrent request limit reached"),
            "retry_after_s": retry_after_s,
            "config_version": version,
        }
        super().__init__(self.detail["error"])


async def _join_cleanup(task: asyncio.Task):
    """Join even through repeated asyncio cancellation or Starlette's cancelled AnyIO scope."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def cleanup_once(action):
    """Keep one owned cleanup task, including if several stream/error paths try to close."""
    task = None

    async def close():
        nonlocal task
        if task is None:
            task = asyncio.create_task(action())
        return await _join_cleanup(task)

    return close


class Slot:
    def __init__(self, store: kv.Store, org_id: int, lease_s: float) -> None:
        self.store = store
        self.key = f"treg:org-concurrency:v1:{org_id}"
        self.token = uuid.uuid4().hex
        self.lease_s = lease_s
        self.ttl_ms = int(lease_s * 1000)
        self.deadline = time.monotonic() + lease_s
        self.renewal: asyncio.Task | None = None
        self._finish_task: asyncio.Task | None = None

    async def _renew(self) -> None:
        warned = False
        while True:
            await asyncio.sleep(self.lease_s / 3)
            started = time.monotonic()
            if started >= self.deadline:
                outcome = "deadline_expired"
            else:
                outcome = await self.store.renew_slot(self.key, self.token, self.ttl_ms)
            if outcome == "renewed" and time.monotonic() < self.deadline:
                self.deadline = started + self.lease_s
                continue
            if not warned:
                log.warning("org_concurrency lease_uncertain outcome=%s; accepted work continues; "
                            "concurrency cap is not guaranteed", outcome)
                warned = True
            if outcome != "unavailable" or time.monotonic() >= self.deadline:
                return  # Never recreate an expired/lost owner or cancel financial cleanup.

    async def _release(self) -> None:
        outcome = await self.store.release_slot(self.key, self.token)
        if outcome == "unavailable":
            log.warning("org_concurrency release_unavailable; owner expires by lease")

    async def finish(self, close=None) -> None:
        async def cleanup():
            try:
                if close is not None:
                    await close()
            except Exception:  # noqa: BLE001 - preserve the original exception in cleanup
                # Preserve the call/stream's original exception instead of replacing it in finally.
                log.warning("org_concurrency upstream_close_failed during cleanup")
            finally:
                # Keep renewing until upstream cleanup has actually finished.
                if self.renewal is not None:
                    self.renewal.cancel()
                    await asyncio.gather(self.renewal, return_exceptions=True)
                await self._release()

        if self._finish_task is None:
            self._finish_task = asyncio.create_task(cleanup())
        await _join_cleanup(self._finish_task)


def _unavailable(org_id: int, version: int) -> None:
    global _next_unavailable_log, _suppressed_unavailable_logs
    now = time.monotonic()
    if now < _next_unavailable_log:
        _suppressed_unavailable_logs += 1
        return
    try:
        log.warning("org_concurrency acquire_unavailable org_id=%s config_version=%s "
                    "action=allow suppressed_since_last_log=%s", org_id, version,
                    _suppressed_unavailable_logs)
    except Exception:  # noqa: BLE001 - logging cannot turn availability into a refusal
        pass  # Logging cannot turn the availability policy into a refusal.
    _suppressed_unavailable_logs = 0
    _next_unavailable_log = now + 5


def _rate_rejected(org_id: int, version: int, rate: flow.RateLimit, retry_after_s: int) -> None:
    global _next_rate_log, _suppressed_rate_logs
    now = time.monotonic()
    if now < _next_rate_log:
        _suppressed_rate_logs += 1
        return
    flow._emit("org_flow_control_rate_limited", org_id, None, level=logging.WARNING,
               status=429, config_version=version, rps=rate.rps, burst=rate.burst,
               retry_after_s=retry_after_s, suppressed_since_last_log=_suppressed_rate_logs)
    _suppressed_rate_logs = 0
    _next_rate_log = now + 5


async def acquire(org_id: int, *, limit: int | None, version: int = 0,
                  rate_limit: flow.RateLimit | None = None) -> Slot | None:
    if limit is None and rate_limit is None:
        return None
    try:
        store = kv.store()
    except Exception:  # noqa: BLE001 - a store that cannot initialize reads as unavailable
        # URL options/client initialization can fail before a command exists to return unavailable.
        return _unavailable(org_id, version)
    slot = Slot(store, org_id, _LEASE_S)
    attempt = asyncio.create_task(
        store.admit_org(org_id, slot.token, slot.ttl_ms, limit,
                        rps=rate_limit.rps, burst=rate_limit.burst)
        if rate_limit is not None else
        store.acquire_slot(slot.key, slot.token, slot.ttl_ms, limit))
    try:
        # Cancelling the caller must not race a Redis write and leave its owner unaccounted for.
        result = await asyncio.shield(attempt)
    except asyncio.CancelledError:
        async def cleanup():
            try:
                await attempt
            finally:
                await slot._release()
        await _join_cleanup(asyncio.create_task(cleanup()))
        raise
    outcome = result.outcome if rate_limit is not None else result
    if outcome == "acquired" and time.monotonic() < slot.deadline:
        if limit is None:
            return None  # Rate admission has no renewable owner and is never refunded.
        slot.renewal = asyncio.create_task(slot._renew())
        return slot
    if outcome == "rate_limited":
        _rate_rejected(org_id, version, rate_limit, result.retry_after_s)
        raise AdmissionRejected(retry_after_s=result.retry_after_s, version=version,
                                rate_limited=True)
    if outcome == "busy":
        try:
            log.warning("org_concurrency rejected org_id=%s limit=%s status=429 config_version=%s",
                        org_id, limit, version)
        except Exception:  # noqa: BLE001 - observability must not replace the refusal
            pass  # Observability must not replace the original refusal.
        raise AdmissionRejected(retry_after_s=_RETRY_AFTER_S, version=version)
    # Includes an acquire reply received after the confirmed lease's conservative deadline.
    await slot.finish()  # A lost reply may have acquired: clean only this unique token.
    return _unavailable(org_id, version)
