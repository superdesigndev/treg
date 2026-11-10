"""Calls in progress on one treg-owned provider account, counted across every server process.

One sorted set per account in the shared store (`infra/kv.py`), on the same lease scripts as org
concurrency: each call owns a member that expires unless renewed, so a crashed process frees its
places within one lease. A lease bounds abandoned occupancy; it cannot stop a request already sent.
No shared store (`TREG_KV_URL` unset) means `enabled()` is false; a store fault reads as `None`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Final, Literal

from . import kv

log = logging.getLogger("treg.provider_concurrency")
_LEASE_S = 30.0
BUSY: Final = "busy"
_pending: set[asyncio.Task] = set()


def enabled() -> bool:
    return kv.configured()


def _background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


class Lease:
    """One place on one account. `release()` is sync and idempotent; the store write runs behind."""

    def __init__(self, store: kv.Store, account: str) -> None:
        self.store = store
        self.key = f"treg:provider-concurrency:v1:{account}"
        self.token = uuid.uuid4().hex
        self.ttl_ms = int(_LEASE_S * 1000)
        # Local clock bound for the Redis-time deadline: set from before the write was sent.
        self.valid_until = time.monotonic() + _LEASE_S
        self.renewal: asyncio.Task | None = None
        self._released = False

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(_LEASE_S / 3)
            started = time.monotonic()
            if started >= self.valid_until:
                outcome = "deadline_expired"
            else:
                outcome = await self.store.renew_slot(self.key, self.token, self.ttl_ms)
            if outcome == "renewed" and time.monotonic() < self.valid_until:
                self.valid_until = started + _LEASE_S
                continue
            if outcome == "unavailable" and time.monotonic() < self.valid_until:
                continue
            # Never recreate an expired owner; the call that holds it continues.
            log.warning("provider_concurrency lease_lost key=%s outcome=%s", self.key, outcome)
            return

    async def _release(self, *, quiet: bool = False) -> None:
        if await self.store.release_slot(self.key, self.token) == "unavailable" and not quiet:
            log.warning("provider_concurrency release_unavailable key=%s; expires by lease", self.key)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.renewal is not None:
            self.renewal.cancel()
        _background(self._release())


async def _drop_after(attempt: asyncio.Future, lease: Lease) -> None:
    """The write may land after the caller left: wait for it, then drop only this token."""
    await asyncio.gather(attempt, return_exceptions=True)
    await lease._release(quiet=True)


async def try_acquire(account: str, cap: int) -> Lease | None | Literal["busy"]:
    """One attempt at a place on `account` among `cap` shared by every process: a renewed `Lease`,
    `BUSY` when every place is taken, `None` when the store does not answer."""
    try:
        store = kv.store()
    except Exception:  # noqa: BLE001 - a store that cannot initialize reads as unavailable
        return None
    lease = Lease(store, account)
    attempt = asyncio.ensure_future(store.acquire_slot(lease.key, lease.token, lease.ttl_ms, cap))
    try:
        outcome = await asyncio.shield(attempt)
    except asyncio.CancelledError:
        _background(_drop_after(attempt, lease))
        raise
    if outcome == "acquired" and time.monotonic() < lease.valid_until:
        lease.renewal = asyncio.get_running_loop().create_task(lease._renew())
        return lease
    if outcome == "busy":
        return BUSY
    # A late "acquired" may name an owner that already expired, and a lost reply may have
    # acquired: drop only this token. A late one asks again; a failing store reads as `None`.
    _background(lease._release(quiet=True))
    return BUSY if outcome == "acquired" else None
