"""Optional Redis admission for root call requests, independent of money admission/locks.

A lease bounds abandoned occupancy; it cannot fence an already-running provider request.
On renewal loss, keep accepted work and its financial cleanup alive and report the lost cap.
The explicit fail-open/closed setting governs *new* admissions on Redis failure only.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import anyio

from ..config import get_settings
from . import kv

log = logging.getLogger("treg.org_concurrency")


class AdmissionRejected(Exception):
    def __init__(self, *, unavailable: bool, retry_after_s: int) -> None:
        self.status_code = 503 if unavailable else 429
        self.retry_after_s = retry_after_s
        self.detail = {
            "error": "org_concurrency_unavailable" if unavailable else "org_concurrency_limit",
            "message": ("organization concurrency admission is unavailable" if unavailable else
                        "organization concurrent request limit reached"),
            "retry_after_s": retry_after_s,
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
            except Exception:
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


def _unavailable(settings) -> None:
    log.warning("org_concurrency acquire_unavailable policy=%s", settings.org_concurrency_redis_failure)
    if settings.org_concurrency_redis_failure != "fail_open":
        raise AdmissionRejected(unavailable=True, retry_after_s=settings.org_concurrency_retry_after_s)


async def acquire(org_id: int) -> Slot | None:
    settings = get_settings()
    limit = settings.org_concurrency_limits.get(org_id)
    if (not settings.org_concurrency_enabled or limit is None
            or org_id in settings.org_concurrency_exempt_org_ids):
        return None
    try:
        store = kv.store()
    except Exception:
        # URL options/client initialization can fail before a command exists to return unavailable.
        return _unavailable(settings)
    slot = Slot(store, org_id, settings.org_concurrency_lease_s)
    attempt = asyncio.create_task(slot.store.acquire_slot(slot.key, slot.token, slot.ttl_ms, limit))
    try:
        # Cancelling the caller must not race a Redis write and leave its owner unaccounted for.
        outcome = await asyncio.shield(attempt)
    except asyncio.CancelledError:
        async def cleanup():
            try:
                await attempt
            finally:
                await slot._release()
        await _join_cleanup(asyncio.create_task(cleanup()))
        raise
    if outcome == "acquired" and time.monotonic() < slot.deadline:
        slot.renewal = asyncio.create_task(slot._renew())
        return slot
    if outcome == "busy":
        try:
            log.warning("org_concurrency rejected org_id=%s limit=%s status=429", org_id, limit)
        except Exception:
            pass  # Observability must not replace the original refusal.
        raise AdmissionRejected(unavailable=False, retry_after_s=settings.org_concurrency_retry_after_s)
    # Includes an acquire reply received after the confirmed lease's conservative deadline.
    await slot.finish()  # A lost reply may have acquired: clean only this unique token.
    return _unavailable(settings)
